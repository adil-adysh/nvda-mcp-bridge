use anyhow::{Context, Result, anyhow, bail};
use reqwest::{Client, Response};
use rmcp::{
    Peer, ServerHandler, ServiceExt,
    model::*,
    service::{NotificationContext, RequestContext, RoleServer, SubscriptionContext},
};
use serde_json::{Map, Value, json};
use std::{
    collections::{HashMap, HashSet},
    env,
    future,
    sync::{
        Arc, RwLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::broadcast;
use tracing::{error, info, warn};
use tracing_subscriber::{EnvFilter, fmt};

mod protocol;
use protocol::{
    API_LEVEL, PROTOCOL_NAME, TOKEN_HEADER,
    BridgeActionEnvelope, BridgeError, BridgeMeta,
    BridgeToolDef, BridgeToolsResponse,
    ErrorCode, NvdaStatusSnapshot,
    validate_tools_payload,
};

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Extract the tool's own `data` schema from an output type envelope.
fn data_schema(output_schema_value: &Value) -> Option<&Value> {
    let inner = output_schema_value
        .as_object()
        .and_then(|obj| obj.get("properties"))
        .and_then(|props| props.as_object())
        .and_then(|props| props.get("data"));
    inner.or(Some(output_schema_value))
}

/// The one resource this companion serves: a snapshot of the NVDA UI state.
const STATE_URI: &str = "nvda://ui/state";

#[derive(Debug, Clone)]
struct Settings {
    bridge_url: String,
    bridge_token: String,
    log_level: String,
}

fn resolve_bridge_token() -> Result<String> {
    if let Ok(token) = env::var("NVDA_BRIDGE_TOKEN") {
        let trimmed = token.trim().to_string();
        if trimmed == "nvda-bridge-secret-change-me" {
            let allow_insecure = env::var("ALLOW_INSECURE_DEFAULT_TOKEN")
                .map(|v| v.trim() == "1")
                .unwrap_or(false);
            if !allow_insecure {
                bail!(
                    "Insecure default bridge token 'nvda-bridge-secret-change-me' is disabled. \
                     Set a secure NVDA_BRIDGE_TOKEN or remove it to read the generated token from %APPDATA%\\nvda\\nvda-mcp-bridge\\token.key."
                );
            }
        }
        if !trimmed.is_empty() {
            return Ok(trimmed);
        }
    }

    // Attempt to discover from user profile directories or explicit configuration
    let mut candidate_paths = Vec::new();

    // 1. Explicit token path override
    if let Ok(token_path) = env::var("NVDA_TOKEN_PATH") {
        let trimmed = token_path.trim().to_string();
        if !trimmed.is_empty() {
            candidate_paths.push(std::path::PathBuf::from(trimmed));
        }
    }

    // 2. Config path override (for portable NVDA or test environments)
    if let Ok(config_path) = env::var("NVDA_CONFIG_PATH") {
        let trimmed = config_path.trim().to_string();
        if !trimmed.is_empty() {
            candidate_paths.push(
                std::path::PathBuf::from(trimmed)
                    .join("nvda-mcp-bridge")
                    .join("token.key"),
            );
        }
    }

    // 3. %APPDATA%\nvda\nvda-mcp-bridge\token.key
    if let Ok(appdata) = env::var("APPDATA") {
        candidate_paths.push(
            std::path::PathBuf::from(appdata)
                .join("nvda")
                .join("nvda-mcp-bridge")
                .join("token.key"),
        );
    }
    // 4. %LOCALAPPDATA%\nvda-mcp-bridge\token.key
    if let Ok(localappdata) = env::var("LOCALAPPDATA") {
        candidate_paths.push(
            std::path::PathBuf::from(localappdata)
                .join("nvda-mcp-bridge")
                .join("token.key"),
        );
    }
    // 5. %USERPROFILE%\.nvda-mcp-bridge\token.key
    if let Ok(userprofile) = env::var("USERPROFILE") {
        candidate_paths.push(
            std::path::PathBuf::from(userprofile)
                .join(".nvda-mcp-bridge")
                .join("token.key"),
        );
    }

    for path in &candidate_paths {
        if path.exists() {
            if let Ok(content) = std::fs::read_to_string(path) {
                let trimmed = content.trim().to_string();
                if trimmed.len() >= 16 {
                    info!(token_path = %path.display(), "Loaded bridge authentication token from key file");
                    return Ok(trimmed);
                }
            }
        }
    }

    bail!(
        "No bridge authentication token found. Set NVDA_BRIDGE_TOKEN or ensure the NVDA add-on \
         has created the token file at %APPDATA%\\nvda\\nvda-mcp-bridge\\token.key"
    )
}

impl Settings {
    fn from_env() -> Result<Self> {
        let bridge_url = env::var("NVDA_BRIDGE_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8765".to_string())
            .trim_end_matches('/')
            .to_string();
        let bridge_token = resolve_bridge_token()?;
        let log_level = env::var("LOG_LEVEL").unwrap_or_else(|_| "INFO".to_string());

        Ok(Self {
            bridge_url,
            bridge_token,
            log_level,
        })
    }
}

/// Extend the bridge data schema with companion metadata.
///
/// Tool data stays at the root. Metadata always has a collision-free `_meta`
/// path; legacy top-level aliases are added only when the tool schema does not
/// already define those fields.
fn extend_output_schema(schema: &Value) -> Value {
    let mut extended = schema.clone();
    let Some(object) = extended.as_object_mut() else {
        return extended;
    };

    let properties_value = object
        .entry("properties")
        .or_insert_with(|| Value::Object(Map::new()));
    if !properties_value.is_object() {
        *properties_value = Value::Object(Map::new());
    }
    let properties = properties_value
        .as_object_mut()
        .expect("properties was normalized to an object");
    let has_tool_result = properties.contains_key("result");
    let has_tool_trace_id = properties.contains_key("trace_id");
    properties.insert(
        "_meta".to_string(),
        json!({
            "type": "object",
            "properties": {
                "result": {},
                "trace_id": {"type": "string"}
            },
            "additionalProperties": false
        }),
    );
    if !has_tool_result {
        properties.insert("result".to_string(), json!({}));
    }
    if !has_tool_trace_id {
        properties.insert("trace_id".to_string(), json!({"type": "string"}));
    }

    let required_value = object
        .entry("required")
        .or_insert_with(|| Value::Array(Vec::new()));
    if let Some(required) = required_value.as_array_mut() {
        if !required.iter().any(|field| field == "_meta") {
            required.push(json!("_meta"));
        }
    } else {
        *required_value = json!(["_meta"]);
    }
    extended
}

/// Encode a tools/list offset as an opaque-enough cursor.
///
/// MCP says the cursor is opaque, not that it must be encrypted. The prefix
/// makes a malformed value distinguishable from an offset that happens to be
/// out of range, so the error can say which mistake was made.
fn encode_tools_cursor(offset: usize) -> String {
    format!("t{offset:x}")
}

fn decode_tools_cursor(cursor: &str) -> Option<usize> {
    cursor
        .strip_prefix('t')
        .filter(|digits| !digits.is_empty())
        .and_then(|digits| usize::from_str_radix(digits, 16).ok())
}

/// Return one page of the catalog and the cursor for the next, if any.
///
/// An unknown cursor is a client error, not an empty result: a client that
/// keeps a cursor past a catalog refresh must re-list from the start rather
/// than silently lose tools.
fn tools_page(
    tools: Vec<Tool>,
    cursor: Option<&str>,
    page_size: usize,
) -> std::result::Result<(Vec<Tool>, Option<String>), ErrorData> {
    let total = tools.len();
    let offset = match cursor {
        None => 0,
        Some(cursor) => decode_tools_cursor(cursor).ok_or_else(|| {
            ErrorData::invalid_params(
                format!("Unknown tools/list cursor: {cursor:?}; re-list without a cursor"),
                None,
            )
        })?,
    };
    if offset > total {
        return Err(ErrorData::invalid_params(
            format!(
                "tools/list cursor points past the end of the catalog ({offset} > {total}); re-list without a cursor"
            ),
            None,
        ));
    }
    let end = offset.saturating_add(page_size).min(total);
    let page = tools
        .into_iter()
        .skip(offset)
        .take(end.saturating_sub(offset))
        .collect();
    let next_cursor = (end < total).then(|| encode_tools_cursor(end));
    Ok((page, next_cursor))
}

/// A human-facing tool title. MCP clients show it in approval UIs, where
/// `get_focus_lineage` is much less readable than `Get Focus Lineage`.
fn human_title(name: &str) -> String {
    const ACRONYMS: &[&str] = &["nvda", "uia", "ui", "mcp", "id"];
    name.split('_')
        .filter(|part| !part.is_empty())
        .map(|part| {
            if ACRONYMS.contains(&part) {
                part.to_ascii_uppercase()
            } else {
                let mut chars = part.chars();
                match chars.next() {
                    Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
                    None => String::new(),
                }
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[derive(Debug, Clone)]
struct ToolRuntime {
    definition: BridgeToolDef,
    input_schema_value: Value,
    output_schema_value: Option<Value>,
    /// The bridge's `data` schema, kept separately so tests and validation can
    /// distinguish it from the extended schema advertised to the client.
    data_schema_value: Option<Value>,
}

#[derive(Debug)]
struct ToolCatalog {
    bridge_version: String,
    /// Identity of the Bridge process that served this catalog. A change means
    /// every cached reference and event cursor from the previous process is
    /// no longer meaningful.
    instance_id: Option<String>,
    schema_hash: Option<String>,
    tools_for_mcp: Vec<Tool>,
    runtime_by_name: HashMap<String, ToolRuntime>,
}

impl ToolCatalog {
    /// A catalog that serves no tools yet.
    ///
    /// Used when the bridge is unreachable at startup: the MCP server still
    /// handshakes and stays alive, so a client keeps its `nvda` server and the
    /// catalog refresh loop can install the real surface when NVDA comes back.
    /// The alternative (exit after five retries) makes the whole toolset vanish
    /// just because NVDA was restarting.
    fn empty(reason: &str) -> Self {
        Self {
            bridge_version: format!("unavailable: {reason}"),
            instance_id: None,
            schema_hash: None,
            tools_for_mcp: Vec::new(),
            runtime_by_name: HashMap::new(),
        }
    }

    /// Drop the advertised `outputSchema`, keeping the runtime copy used for
    /// validation and `get_tool_schema(include_output_schema=true)`.
    fn omit_output_schemas(&mut self) {
        for tool in &mut self.tools_for_mcp {
            tool.output_schema = None;
        }
    }
}

#[derive(Debug, Clone)]
struct BridgeClient {
    client: Client,
    base_url: String,
    auth_token: String,
    /// Configured per-request deadline, extended only for bounded speech waits.
    request_timeout: Duration,
    /// Category allow-list for the catalog, from `NVDA_BRIDGE_CATEGORIES`.
    ///
    /// The catalog is context: every tool's schema is paid for by the agent on
    /// every turn. A companion serving a production agent can ask the bridge for
    /// actions and observations only, and never load the diagnostics.
    categories: Option<String>,
    /// Whether `tools/list` advertises each tool's `outputSchema`.
    ///
    /// Output schemas are documentation: `structuredContent` is still returned
    /// and still validated against the bridge's data model. They are roughly
    /// half of the catalog bytes, so they are opt-in rather than paid for on
    /// every client that never reads them.
    include_output_schemas: bool,
}

impl BridgeClient {
    fn next_request_id() -> String {
        let sequence = REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);

        format!("mcp-{}-{}", millis, sequence)
    }

    fn new(base_url: String, auth_token: String) -> Result<Self> {
        if base_url.trim().is_empty() {
            bail!("bridge_url cannot be empty");
        }
        if auth_token.trim().is_empty() {
            bail!("auth_token cannot be empty");
        }

        let timeout_secs = env::var("NVDA_BRIDGE_TIMEOUT_SECS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(15);

        let request_timeout = Duration::from_secs(timeout_secs);
        let client = Client::builder()
            .timeout(request_timeout)
            .build()
            .context("Failed to build HTTP client")?;

        let categories = env::var("NVDA_BRIDGE_CATEGORIES").ok().and_then(|raw| {
            let kept: Vec<&str> = raw
                .split(',')
                .map(str::trim)
                .filter(|name| {
                    !name.is_empty()
                        && name
                            .chars()
                            .all(|c| c.is_ascii_lowercase() || c == '_' || c == '-')
                })
                .collect();
            if kept.is_empty() {
                None
            } else {
                Some(kept.join(","))
            }
        });
        if categories.is_some() {
            info!(
                categories = %categories.as_deref().unwrap_or(""),
                "Only these tool categories will be advertised"
            );
        }

        let include_output_schemas = env::var("NVDA_BRIDGE_OUTPUT_SCHEMAS")
            .ok()
            .map(|raw| {
                matches!(
                    raw.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(false);
        info!(
            include_output_schemas,
            "Output schemas are {} in tools/list", if include_output_schemas { "included" } else { "omitted" }
        );

        Ok(Self {
            client,
            base_url,
            auth_token,
            request_timeout,
            categories,
            include_output_schemas,
        })
    }

    async fn health_check(&self) -> bool {
        let url = format!("{}/health", self.base_url);
        match self.client.get(url).send().await {
            Ok(response) => {
                let healthy = response.status().is_success();
                info!(status = %response.status(), healthy, "Bridge health check complete");
                healthy
            }
            Err(err) => {
                warn!(error = %err, "Bridge health check failed");
                false
            }
        }
    }

    async fn fetch_tool_catalog(&self) -> Result<ToolCatalog> {
        let url = match &self.categories {
            Some(categories) => format!("{}/tools?category={}", self.base_url, categories),
            None => format!("{}/tools", self.base_url),
        };
        let response = self
            .client
            .get(url)
            .header(TOKEN_HEADER, &self.auth_token)
            .send()
            .await
            .context("Failed to connect to bridge tools endpoint")?;

        let status = response.status();
        let body_text = response.text().await.unwrap_or_default();

        if status.as_u16() == 401 {
            bail!("Invalid authentication token for bridge");
        }
        if !status.is_success() {
            bail!(
                "Bridge tools discovery failed with {}: {}",
                status.as_u16(),
                Self::extract_error_message(&body_text)
            );
        }

        let payload: BridgeToolsResponse = serde_json::from_str(&body_text)
            .context("Bridge /tools response had unexpected JSON shape")?;

        validate_tools_payload(
            &payload.protocol,
            &payload.protocol_version,
            &payload.bridge_version,
            payload.api_level,
            payload.supported_api_levels.as_deref().unwrap_or(&[]),
            payload.count,
            payload.tools.len(),
        ).map_err(|msg| anyhow!("{}", msg))?;
        let mut catalog = Self::build_catalog(payload)?;
        if !self.include_output_schemas {
            catalog.omit_output_schemas();
        }
        Ok(catalog)
    }

    fn build_catalog(payload: BridgeToolsResponse) -> Result<ToolCatalog> {
        let mut tools_for_mcp = Vec::with_capacity(payload.tools.len());
        let mut runtime_by_name = HashMap::with_capacity(payload.tools.len());
        let mut seen = HashSet::with_capacity(payload.tools.len());

        for def in payload.tools {
            let name = def.name.trim();
            if name.is_empty() {
                bail!("Bridge returned a tool with empty name");
            }
            if !seen.insert(name.to_string()) {
                bail!("Bridge returned duplicate tool name: {}", name);
            }

            let input_schema_value = Value::Object(def.input_schema.clone());

            let output_schema_value = if let Some(output_type_name) = &def.output_type {
                match payload.types.get(output_type_name) {
                    Some(schema) => {
                        Some(schema.clone())
                    }
                    None => match &def.output_schema {
                        Some(schema) => Some(Value::Object(schema.clone())),
                        None => {
                            warn!(
                                tool = %def.name,
                                output_type = %output_type_name,
                                "Tool outputType not found in bridge types map and no outputSchema was provided"
                            );
                            None
                        }
                    },
                }
            } else {
                def.output_schema.clone().map(Value::Object)
            };

            // The bridge publishes an envelope schema (`{success, data, error}`)
            // in its types map. structuredContent keeps the data fields at the
            // root and adds companion metadata under `_meta`.
            let data_schema_value = output_schema_value
                .as_ref()
                .and_then(data_schema)
                .cloned();
            let extended_schema = data_schema_value.as_ref().map(extend_output_schema);

            let mcp_tool = {
                let mut tool = Tool::new(
                    def.name.clone(),
                    def.description.clone(),
                    def.input_schema.clone(),
                )
                .annotate(annotation_for_tool(&def));
                // Group the display title without renaming the tool. Approval
                // lists and pickers then show "Diagnostics · Health Snapshot"
                // instead of a flat list of 30+ undifferentiated names.
                tool.title = Some(if def.category.is_empty() {
                    human_title(&def.name)
                } else {
                    format!("{} · {}", human_title(&def.category), human_title(&def.name))
                });
                // Advertise the exact schema the companion validates and sends.
                tool.output_schema = extended_schema
                    .as_ref()
                    .and_then(|schema| schema.as_object().cloned())
                    .map(Arc::new);
                tool
            };


            info!(
                tool = %def.name,
                category = %def.category,
                version = %def.version,
                "Loaded tool from bridge schema"
            );

            tools_for_mcp.push(mcp_tool);
            runtime_by_name.insert(
                def.name.clone(),
                ToolRuntime {
                    definition: def,
                    input_schema_value,
                    output_schema_value: extended_schema,
                    data_schema_value,
                },
            );
        }

        Ok(ToolCatalog {
            bridge_version: payload.bridge_version,
            instance_id: payload.instance_id,
            schema_hash: payload.schema_hash,
            tools_for_mcp,
            runtime_by_name,
        })
    }

    /// Fetch one bridge document (``/state``, ``/events``) as raw JSON.
    async fn get_json(&self, path_and_query: &str) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path_and_query);
        let response = self
            .client
            .get(&url)
            .header(TOKEN_HEADER, &self.auth_token)
            .send()
            .await
            .with_context(|| format!("Failed to connect to bridge for {path_and_query}"))?;

        let status = response.status();
        let body_text = response.text().await.unwrap_or_default();
        if status.as_u16() == 401 {
            bail!("Invalid authentication token for bridge");
        }
        if status.as_u16() >= 400 {
            bail!(
                "Bridge returned {} for {}: {}",
                status.as_u16(),
                path_and_query,
                Self::extract_error_message(&body_text)
            );
        }
        serde_json::from_str(&body_text)
            .with_context(|| format!("Bridge returned non-JSON for {path_and_query}"))
    }

    async fn execute_action(&self, action: &str, params: Map<String, Value>) -> Result<BridgeActionEnvelope> {
        let action_timeout = Self::request_timeout_for_tool(action, &params, self.request_timeout);
        let request_id = Self::next_request_id();
        let payload = json!({
            "request_id": request_id,
            "action": action,
            "params": params,
        });

        let url = format!("{}/action", self.base_url);
        let mut request = self
            .client
            .post(&url)
            .header(TOKEN_HEADER, &self.auth_token)
            .header(reqwest::header::CONTENT_TYPE, "application/json");
        if let Some(timeout) = action_timeout {
            request = request.timeout(timeout);
        }
        let response = request
            .json(&payload)
            .send()
            .await
            .with_context(|| format!("Failed to connect to bridge for action: {action}"))?;

        Self::parse_action_response(action, response).await
    }

    fn request_timeout_for_tool(
        action: &str,
        params: &Map<String, Value>,
        configured_timeout: Duration,
    ) -> Option<Duration> {
        if action != "wait_for_speech" {
            return None;
        }
        let wait_ms = params
            .get("timeout_ms")
            .and_then(Value::as_u64)
            .unwrap_or(1_000)
            .min(30_000);
        let speech_deadline = Duration::from_millis(wait_ms.saturating_add(5_000));
        Some(configured_timeout.max(speech_deadline))
    }

    async fn parse_action_response(action: &str, response: Response) -> Result<BridgeActionEnvelope> {
        let status = response.status();
        let body_text = response.text().await.unwrap_or_default();

        if status.as_u16() == 401 {
            bail!("Invalid authentication token for bridge");
        }

        if status.as_u16() >= 400 {
            // The bridge reports domain failures (a stale reference, a window that
            // does not exist, a dialog in the way) as 4xx with a complete error
            // envelope. That is an answer, not a transport failure: passing it
            // through gives the caller the code, the retry policy and the details
            // instead of "bridge unreachable", which invites pointless retries.
            if status.as_u16() != 401 {
                if let Ok(parsed) = serde_json::from_str::<Value>(&body_text) {
                    if parsed.get("success").and_then(Value::as_bool) == Some(false) {
                        return Self::parse_action_payload(parsed);
                    }
                }
            }
            bail!(
                "Bridge returned {} for action '{}': {}",
                status.as_u16(),
                action,
                Self::extract_error_message(&body_text)
            );
        }

        let parsed: Value = serde_json::from_str(&body_text)
            .with_context(|| format!("Invalid JSON response for action: {action}"))?;
        Self::parse_action_payload(parsed)
    }

    fn parse_action_payload(payload: Value) -> Result<BridgeActionEnvelope> {
        let Some(obj) = payload.as_object() else {
            bail!("Bridge action response must be a JSON object");
        };

        let success = obj
            .get("success")
            .and_then(Value::as_bool)
            .ok_or_else(|| anyhow!("Bridge action response missing required 'success' boolean"))?;

        // Extract protocol metadata for downstream AI agent observability
        let request_id = obj.get("request_id").and_then(Value::as_str).map(String::from);
        let action = obj.get("action").and_then(Value::as_str).map(String::from);
        let duration_ms = obj.get("duration_ms").and_then(Value::as_f64);
        let bridge: Option<BridgeMeta> = obj
            .get("bridge")
            .and_then(|v| serde_json::from_value(v.clone()).ok());
        let nvda_status_snapshot: Option<NvdaStatusSnapshot> = obj
            .get("nvda_status_snapshot")
            .and_then(|v| serde_json::from_value(v.clone()).ok());
        let result = obj.get("result").cloned();
        // The diagnostic identifiers travel with every envelope, success or
        // failure: losing them would leave an agent unable to ask why.
        let trace_id = obj.get("trace_id").and_then(Value::as_str).map(String::from);
        let trace = obj.get("trace").cloned();

        if success {
            let data = obj
                .get("data")
                .cloned()
                .or_else(|| obj.get("result").cloned())
                .or_else(|| Some(Value::Object(Map::new())));
            return Ok(BridgeActionEnvelope {
                success: true,
                request_id,
                action,
                duration_ms,
                bridge,
                nvda_status_snapshot,
                data,
                error: None,
                result,
                trace_id,
                trace,
            });
        }

        let parsed_error: BridgeError = obj
            .get("error")
            .and_then(|v| Self::parse_bridge_error(v))
            .unwrap_or_else(|| BridgeError {
                code: ErrorCode("BRIDGE_ERROR".into()),
                message: "Bridge returned unsuccessful response without error details".into(),
                name: None,
                category: None,
                retry_policy: None,
                retry_after_ms: None,
                details: None,
                recovery: None,
            });

        Ok(BridgeActionEnvelope {
            success: false,
            request_id,
            action,
            duration_ms,
            bridge,
            nvda_status_snapshot,
            data: obj.get("data").cloned(),
            error: Some(parsed_error),
            result,
            trace_id,
            trace,
        })
    }

    /// Parse a structured bridge error object from JSON.
    fn parse_bridge_error(val: &Value) -> Option<BridgeError> {
        let obj = val.as_object()?;
        let code = obj
            .get("code")
            .and_then(|c| {
                c.as_i64()
                    .map(|i| ErrorCode(i.to_string()))
                    .or_else(|| c.as_str().map(|s| ErrorCode(s.to_string())))
            })
            .unwrap_or(ErrorCode("BRIDGE_ERROR".into()));
        let message = obj
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("Unknown bridge error")
            .to_string();
        let name = obj.get("name").and_then(Value::as_str).map(String::from);
        let category = obj.get("category").and_then(Value::as_str).map(String::from);
        let retry_policy = obj.get("retry_policy").and_then(Value::as_str).map(String::from);
        let retry_after_ms = obj.get("retry_after_ms").and_then(Value::as_u64);
        let details = obj.get("details").cloned();
        // Recovery is the one field an agent can act on without interpreting
        // prose; dropping it here is why the wire had it but MCP did not.
        let recovery = obj.get("recovery").cloned();
        Some(BridgeError {
            code,
            message,
            name,
            category,
            retry_policy,
            retry_after_ms,
            details,
            recovery,
        })
    }

    fn extract_error_message(body_text: &str) -> String {
        if let Ok(parsed) = serde_json::from_str::<Value>(body_text) {
            if let Some(obj) = parsed.as_object() {
                if let Some(error_obj) = obj.get("error").and_then(Value::as_object) {
                    if let Some(message_text) = error_obj.get("message").and_then(Value::as_str) {
                        return format!(
                            "[{}] {}",
                            error_obj
                                .get("code")
                                .map(|c| c.to_string())
                                .unwrap_or_default(),
                            message_text
                        );
                    }
                }
                if let Some(error_text) = obj.get("error").and_then(Value::as_str) {
                    return error_text.to_string();
                }
                if let Some(message_text) = obj.get("message").and_then(Value::as_str) {
                    return message_text.to_string();
                }
                return parsed.to_string();
            }

            return parsed.to_string();
        }

        body_text.to_string()
    }
}

fn annotation_for_tool(tool: &BridgeToolDef) -> ToolAnnotations {
    let annotation = ToolAnnotations::new().open_world(false);

    let read_only = tool.read_only.unwrap_or_else(|| {
        tool.name.starts_with("get_")
            || tool.name.starts_with("read_")
            || tool.name.starts_with("inspect_")
            || tool.name.starts_with("walk_")
            || tool.name.starts_with("list_")
            || tool.name.starts_with("health_")
            || matches!(
                tool.category.as_str(),
                "diagnostics" | "navigation" | "focus" | "window" | "text"
            )
    });

    let destructive = tool.destructive.unwrap_or_else(|| {
        tool.constraints
            .as_ref()
            .and_then(|c| c.dangerous)
            .unwrap_or(false)
    });

    let idempotent = tool.idempotent.unwrap_or(read_only);

    annotation
        .read_only(read_only)
        .destructive(destructive)
        .idempotent(idempotent)
}

struct NvdaServer {
    bridge_client: BridgeClient,
    tool_catalog: Arc<RwLock<ToolCatalog>>,
    /// Filled in by `on_initialized`: the refresh loop needs a handle on the
    /// connected client to announce that the tool surface changed.
    peer: Arc<RwLock<Option<Peer<RoleServer>>>>,
    subscription_tx: broadcast::Sender<i64>,
    shutdown: Arc<AtomicBool>,
    /// Maximum tools returned by one `tools/list` page.
    ///
    /// MCP pagination is a transport contract, not a token-saving feature: a
    /// client that needs every tool still walks every page. The default is
    /// high enough that today's catalog is one page, so behaviour only changes
    /// when a caller asks (or a deployment opts in with a smaller value).
    tools_page_size: usize,
}

impl NvdaServer {
    fn new(bridge_client: BridgeClient, tool_catalog: ToolCatalog) -> Self {
        Self::with_catalog(bridge_client, Arc::new(RwLock::new(tool_catalog)))
    }

    fn with_catalog(bridge_client: BridgeClient, tool_catalog: Arc<RwLock<ToolCatalog>>) -> Self {
        let (subscription_tx, _) = broadcast::channel(64);
        let tools_page_size = env::var("NVDA_BRIDGE_TOOLS_PAGE_SIZE")
            .ok()
            .and_then(|raw| raw.trim().parse::<usize>().ok())
            .unwrap_or(100)
            .clamp(1, 1000);
        Self {
            bridge_client,
            tool_catalog,
            peer: Arc::new(RwLock::new(None)),
            subscription_tx,
            shutdown: Arc::new(AtomicBool::new(false)),
            tools_page_size,
        }
    }

    /// Clone the current tool runtime so callers can drop the catalog read lock
    /// before any `.await`.
    fn tool_runtime(&self, name: &str) -> Option<ToolRuntime> {
        let catalog = self.catalog_read();
        catalog.runtime_by_name.get(name).cloned()
    }

    fn catalog_read(&self) -> std::sync::RwLockReadGuard<'_, ToolCatalog> {
        self.tool_catalog
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn validate_input(
        runtime: &ToolRuntime,
        params: &Map<String, Value>,
    ) -> std::result::Result<(), Map<String, Value>> {
        let args_value = Value::Object(params.clone());
        let Ok(validator) = jsonschema::validator_for(&runtime.input_schema_value) else {
            // A schema the validator cannot compile is a companion bug, not a
            // caller error; report it as such rather than as bad arguments.
            let mut errors = Map::new();
            errors.insert(
                "<schema>".to_string(),
                json!("the tool inputSchema could not be compiled"),
            );
            return Err(errors);
        };

        let mut field_errors = Map::new();
        for error in validator.iter_errors(&args_value) {
            let path = error.instance_path().to_string();
            let key = if path.is_empty() {
                "<root>".to_string()
            } else {
                path
            };
            field_errors.insert(key, json!(error.to_string()));
        }
        if field_errors.is_empty() {
            Ok(())
        } else {
            Err(field_errors)
        }
    }

    /// Validate the `data` portion of a bridge response against the inner output schema.
    ///
    /// The output type schemas in `schema.json` define `{success, data: {...}, error}` envelopes,
    /// while the actual HTTP response also carries transport metadata (`request_id`, `action`,
    fn validate_output(runtime: &ToolRuntime, payload: &BridgeActionEnvelope) -> Result<()> {
        if !payload.success {
            return Ok(());
        }
        let Some(output_schema_value) = &runtime.output_schema_value else {
            return Ok(());
        };

        let data_value = payload
            .data
            .clone()
            .unwrap_or_else(|| Value::Object(Map::new()));

        // The advertised schema is the bridge's data model plus the companion's
        // optional metadata. Validate the bridge-owned part against the bridge's
        // own schema, which is stricter than quietly accepting anything.
        let inner_schema = runtime
            .data_schema_value
            .as_ref()
            .unwrap_or(output_schema_value);

        if jsonschema::is_valid(inner_schema, &data_value) {
            return Ok(());
        }

        // Fallback: try the full envelope schema (legacy)
        let envelope_value = json!({
            "success": true,
            "data": data_value,
        });
        if jsonschema::is_valid(output_schema_value, &envelope_value) {
            return Ok(());
        }

        bail!(
            "Bridge output validation failed for '{}': response data does not match declared outputType '{}'",
            runtime.definition.name,
            runtime.definition.output_type.as_deref().unwrap_or("(none)")
        );
    }

    /// Build the MCP result for a successful bridge response.
    ///
    /// `structuredContent` keeps the bridge's data fields at the root and puts
    /// companion-owned metadata under `_meta`. Legacy top-level metadata aliases
    /// remain for fields the bridge data does not already own.
    ///
    /// The text block is the same compact JSON, not a second pretty-printed
    /// envelope: one representation, duplicated only for clients that cannot
    /// read structured content. The verbose per-call `trace` object is gone;
    /// callers use `get_action_trace` with `trace_id` when they need it.
    fn success_result(payload: &BridgeActionEnvelope) -> CallToolResult {
        let mut structured = match payload.data.clone() {
            Some(Value::Object(map)) => Value::Object(map),
            Some(other) => json!({ "value": other }),
            None => Value::Object(Map::new()),
        };
        let mut metadata = Map::new();
        if let Some(ref result) = payload.result {
            metadata.insert("result".to_string(), result.clone());
        }
        if let Some(ref trace_id) = payload.trace_id {
            metadata.insert("trace_id".to_string(), json!(trace_id));
        }
        if let Some(object) = structured.as_object_mut() {
            if let Some(ref result) = payload.result {
                object
                    .entry("result".to_string())
                    .or_insert_with(|| result.clone());
            }
            if let Some(ref trace_id) = payload.trace_id {
                object
                    .entry("trace_id".to_string())
                    .or_insert_with(|| json!(trace_id));
            }
            object.insert("_meta".to_string(), Value::Object(metadata));
        }

        let text = serde_json::to_string(&structured).unwrap_or_default();
        let mut result = CallToolResult::success(vec![ContentBlock::text(text)]);
        result.structured_content = Some(structured);
        result
    }

    /// Build the MCP result for a failed bridge response.
    ///
    /// The error is a compact JSON object in the text block: valid JSON (so a
    /// text-only client can parse it) with `code`, `name`, `category`,
    /// `retry_policy`, `recovery` and field details. No `structuredContent` is
    /// attached, because the tool's `outputSchema` describes a successful
    /// payload; attaching an error envelope there would violate it.
    fn error_result(payload: &BridgeActionEnvelope) -> CallToolResult {
        let err = payload.error.clone().unwrap_or(BridgeError {
            code: ErrorCode("BRIDGE_ERROR".into()),
            message: "Unknown bridge error".into(),
            name: None,
            category: None,
            retry_policy: None,
            retry_after_ms: None,
            details: None,
            recovery: None,
        });

        let code_display = err.code.as_str();
        let mut detail = json!({
            "success": false,
            "error": {
                "code": code_display,
                "message": err.message,
            }
        });

        if let Some(ref name) = err.name {
            detail["error"]["name"] = json!(name);
        }
        if let Some(ref cat) = err.category {
            detail["error"]["category"] = json!(cat);
        }
        if let Some(ref rp) = err.retry_policy {
            detail["error"]["retry_policy"] = json!(rp);
        }
        if let Some(ram) = err.retry_after_ms {
            detail["error"]["retry_after_ms"] = json!(ram);
        }
        if let Some(ref det) = err.details {
            detail["error"]["details"] = det.clone();
        }
        // Recovery is a typed instruction, not prose the model has to infer.
        // It never authorises an automatic replay of a mutation.
        if let Some(ref recovery) = err.recovery {
            detail["error"]["recovery"] = recovery.clone();
        }
        if let Some(ref result) = payload.result {
            detail["result"] = result.clone();
        }
        // A failed action's trace id is at least as valuable as a successful
        // one's: it is how the caller asks why it failed.
        if let Some(ref trace_id) = payload.trace_id {
            detail["trace_id"] = json!(trace_id);
        }
        if let Some(ref rid) = payload.request_id {
            detail["request_id"] = json!(rid);
        }

        CallToolResult::error(vec![ContentBlock::text(
            serde_json::to_string(&detail).unwrap_or_default(),
        )])
    }

    /// Build the MCP result for an argument that failed local validation.
    ///
    /// Uses the bridge's 4103 vocabulary so a client sees the same code whether
    /// the companion or the bridge rejected the input, and names the offending
    /// fields instead of a generic "does not match inputSchema".
    fn input_error_result(tool_name: &str, field_errors: Map<String, Value>) -> CallToolResult {
        let detail = json!({
            "success": false,
            "error": {
                "code": "4103",
                "protocol_code": 4103,
                "name": "INVALID_PARAMS_SCHEMA",
                "category": "validation",
                "retry_policy": "fix_arguments",
                "message": format!(
                    "Input validation failed for '{}': {} field(s) do not match inputSchema",
                    tool_name,
                    field_errors.len()
                ),
                "details": {
                    "error_code": "invalid_input_schema",
                    "tool_name": tool_name,
                    "field_errors": field_errors,
                },
                "recovery": {
                    "action": "fix_arguments",
                    "hint": "Correct the named parameters; no UI action was performed."
                }
            }
        });
        CallToolResult::error(vec![ContentBlock::text(
            serde_json::to_string(&detail).unwrap_or_default(),
        )])
    }
}

impl Drop for NvdaServer {
    fn drop(&mut self) {
        // Signal the background event pump to exit. It holds only a clone of the
        // broadcast sender and the HTTP client, not the server itself.
        self.shutdown.store(true, Ordering::SeqCst);
    }
}

impl ServerHandler for NvdaServer {
    fn get_info(&self) -> ServerConfig {
        let bridge_version = self.catalog_read().bridge_version.clone();
        // Advertise resources and subscriptions as well as tools: the bridge
        // exposes live NVDA state as resources, and clients that support
        // `subscriptions/listen` can be notified when it changes.
        ServerConfig::new(
            ServerCapabilities::builder()
                .enable_tools()
                // The catalog refresh loop swaps in a new surface when the
                // add-on's `schema_hash` changes; promising list-changed is
                // what makes a client re-list instead of keeping the tool set
                // it saw at connect time.
                .enable_tool_list_changed()
                .enable_resources()
                // `subscribe: true` is what lets the SDK accept a
                // resourceSubscriptions filter on subscriptions/listen.
                .enable_resources_subscribe()
                .build(),
        )
        .with_server_info(Implementation::new(
            "nvda-mcp-server-rust",
            env!("CARGO_PKG_VERSION"),
        ))
        .with_instructions(format!(
            "Bridge-backed tools discovered from /tools. protocol={} api_level={} bridge_version={}",
            PROTOCOL_NAME, API_LEVEL, bridge_version
        ))
    }

    fn on_initialized(
        &self,
        context: NotificationContext<RoleServer>,
    ) -> impl std::future::Future<Output = ()> + Send + '_ {
        // Keep the peer so the refresh loop can tell this client that the tool
        // surface changed. A client that has gone away is not a problem: the
        // send fails and the loop logs it.
        let mut slot = self
            .peer
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *slot = Some(context.peer);
        future::ready(())
    }

    fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<ListToolsResult, ErrorData>> + Send + '_ {
        let tools = self.catalog_read().tools_for_mcp.clone();
        let cursor = request.and_then(|params| params.cursor);
        future::ready(
            tools_page(tools, cursor.as_deref(), self.tools_page_size).map(|(tools, next_cursor)| {
                ListToolsResult {
                    tools,
                    next_cursor,
                    ..Default::default()
                }
            }),
        )
    }

    fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<ListResourcesResult, ErrorData>> + Send + '_ {
        future::ready(Ok(ListResourcesResult::with_all_items(vec![
            Resource::new(STATE_URI, "NVDA UI state")
                .with_description(
                    "Versioned snapshot of the focused element, foreground window and NVDA runtime state.",
                )
                .with_mime_type("application/json"),
        ])))
    }

    fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<ReadResourceResponse, ErrorData>> + Send + '_ {
        async move {
            if request.uri != STATE_URI {
                return Err(ErrorData::resource_not_found(
                    format!("Unknown resource: {}", request.uri),
                    None,
                ));
            }

            let state = self
                .bridge_client
                .get_json("/state")
                .await
                .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;

            Ok(ReadResourceResponse::Complete(ReadResourceResult::new(vec![
                ResourceContents::text(serde_json::to_string_pretty(&state).unwrap_or_default(), STATE_URI)
                    .with_mime_type("application/json"),
            ])))
        }
    }

    fn accepted_subscription_filter(&self, requested: &SubscriptionFilter) -> Option<SubscriptionFilter> {
        // Accept only the state resource. Change notifications are emitted after
        // the bridge journal advances, so this is the only URI we can serve.
        let requested_uris = requested.resource_subscriptions.as_deref().unwrap_or(&[]);
        let accepted_uris: Vec<String> = requested_uris
            .iter()
            .filter(|uri| uri.as_str() == STATE_URI)
            .cloned()
            .collect();

        if accepted_uris.is_empty() {
            return None;
        }

        Some(
            SubscriptionFilter::builder()
                .resource_subscriptions(accepted_uris)
                .build(),
        )
    }

    fn listen(
        &self,
        context: SubscriptionContext,
    ) -> impl std::future::Future<Output = std::result::Result<(), ErrorData>> + Send + '_ {
        async move {
            // All subscribers share the single `/events` pump started in `main()`;
            // a subscription only waits on its broadcast receiver.
            let mut updates = self.subscription_tx.subscribe();
            loop {
                tokio::select! {
                    _ = context.cancelled() => return Ok(()),
                    received = updates.recv() => {
                        match received {
                            Ok(_version) => {
                                let notification = ServerNotification::ResourceUpdatedNotification(
                                    ResourceUpdatedNotification::new(ResourceUpdatedNotificationParam::new(
                                        STATE_URI,
                                    )),
                                );
                                // A dead or stuck client must not hold the loop forever.
                                match tokio::time::timeout(
                                    Duration::from_secs(10),
                                    context.sink().send(notification),
                                ).await {
                                    Ok(Ok(())) => {}
                                    Ok(Err(_)) | Err(_) => return Ok(()),
                                }
                            }
                            Err(broadcast::error::RecvError::Lagged(_)) => continue,
                            Err(broadcast::error::RecvError::Closed) => return Ok(()),
                        }
                    }
                }
            }
        }
    }

    fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<CallToolResponse, ErrorData>> + Send + '_ {
        async move {
            let tool_name = request.name.to_string();
            // Clone the runtime and release the catalog lock before any `.await`.
            let Some(runtime) = self.tool_runtime(&tool_name) else {
                return Err(ErrorData::invalid_params(
                    format!("Unknown tool: {}", tool_name),
                    None,
                ));
            };

            if runtime
                .definition
                .constraints
                .as_ref()
                .and_then(|c| c.requires_explicit_opt_in)
                .unwrap_or(false)
            {
                warn!(
                    tool = %tool_name,
                    "Executing opt-in tool; client must ensure user approved this action"
                );
            }

            let params = request.arguments.unwrap_or_default();

            if let Err(field_errors) = Self::validate_input(&runtime, &params) {
                return Ok(CallToolResponse::Complete(Self::input_error_result(
                    &tool_name,
                    field_errors,
                )));
            }

            let bridge_result = match self.bridge_client.execute_action(&tool_name, params).await {
                Ok(result) => result,
                Err(err) => {
                    error!(tool = %tool_name, error = %err, "Bridge call failed");
                    let error_envelope = BridgeActionEnvelope {
                        success: false,
                        request_id: None,
                        action: Some(tool_name.clone()),
                        duration_ms: None,
                        bridge: None,
                        nvda_status_snapshot: None,
                        data: None,
                        error: Some(BridgeError {
                            code: ErrorCode("BRIDGE_UNREACHABLE".into()),
                            message: format!("Bridge communication failure: {}", err),
                            name: Some("BridgeConnectionError".into()),
                            category: Some("transport".into()),
                            retry_policy: Some("no_retry".into()),
                            retry_after_ms: None,
                            details: None,
                            recovery: None,
                        }),
                        result: None,
                        trace_id: None,
                        trace: None,
                    };
                    return Ok(CallToolResponse::Complete(Self::error_result(
                        &error_envelope,
                    )));
                }
            };

            if let Err(err) = Self::validate_output(&runtime, &bridge_result) {
                error!(tool = %tool_name, error = %err, "Bridge output schema mismatch");
                return Ok(CallToolResponse::Complete(CallToolResult::error(vec![
                    ContentBlock::text(err.to_string()),
                ])));
            }

            if bridge_result.success {
                Ok(CallToolResponse::Complete(Self::success_result(&bridge_result)))
            } else {
                Ok(CallToolResponse::Complete(Self::error_result(&bridge_result)))
            }
        }
    }
}

/// Whether a freshly fetched catalog represents a new contract.
///
/// A hash appearing or disappearing is treated as a change; two absent hashes
/// cannot prove anything, so they never trigger a swap.
fn schema_hash_changed(current: Option<&str>, incoming: Option<&str>) -> bool {
    match (current, incoming) {
        (Some(current), Some(incoming)) => current != incoming,
        (None, None) => false,
        _ => true,
    }
}

/// Whether the Bridge process serving the catalog changed.
///
/// An absent id on either side is "unknown", not "same": a replacement that
/// cannot be identified must not be silently treated as continuity. An id that
/// appears where there was none is a reason to refresh, because the previous
/// catalog came from a bridge too old to identify itself.
fn bridge_replaced(current: Option<&str>, incoming: Option<&str>) -> bool {
    match (current, incoming) {
        (Some(current), Some(incoming)) => current != incoming,
        (None, None) => false,
        _ => true,
    }
}

/// Resolve the catalog refresh period from `NVDA_BRIDGE_CATALOG_REFRESH_SECS`.
/// `0` disables periodic refreshes.
fn catalog_refresh_period() -> Option<Duration> {
    let secs = env::var("NVDA_BRIDGE_CATALOG_REFRESH_SECS")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .unwrap_or(15);
    (secs > 0).then(|| Duration::from_secs(secs))
}

/// What one polled page means for the cursor.
#[derive(Debug, PartialEq, Eq)]
enum PumpStep {
    /// The journal was replaced, or the cursor outlived it: start over from the
    /// page's head and tell subscribers to re-read the state.
    Restart,
    /// The same journal, moving forward.
    Advance,
    /// Nothing new.
    Idle,
}

/// Decide what to do with one polled page.
///
/// A sequence number only means something inside the journal that produced it.
/// An NVDA restart begins again at zero, so carrying the old cursor across one
/// would suppress every notification until the new sequence outgrew the old.
/// The journal's id is what makes that detectable; a version that goes backwards
/// is the same fact when the bridge is too old to send an id.
fn pump_step(previous_journal: Option<&str>, seen_journal: Option<&str>, version: i64, cursor: i64) -> PumpStep {
    let replaced = matches!(
        (previous_journal, seen_journal),
        (Some(known), Some(seen)) if known != seen
    );
    if replaced || version < cursor {
        PumpStep::Restart
    } else if version > cursor {
        PumpStep::Advance
    } else {
        PumpStep::Idle
    }
}

/// What one page does to the cursor, and whether another page is already due.
#[derive(Debug, PartialEq, Eq)]
struct PageOutcome {
    step: PumpStep,
    /// The cursor to poll from next.
    cursor: i64,
    /// True when the bridge said `has_more` and this page actually moved the
    /// cursor, so the next page can be fetched immediately.
    more_pages: bool,
}

/// Advance the cursor to the last event this page actually delivered.
///
/// The bridge reports two numbers: `to_version` (the last event in the page) and
/// `state_version`/`event_version` (the journal head). Advancing to the head
/// while `has_more` is true would skip everything between the page and the head
/// with no record that it happened; advancing to `to_version` walks the journal
/// one page at a time. Bridges too old to send `to_version` only ever returned
/// the newest page, so the head is the right value for them.
///
/// `has_more` with no progress would spin the pump at full speed against an
/// unresponsive bridge, so that case falls back to the normal long poll.
fn consume_page(
    previous_journal: Option<&str>,
    seen_journal: Option<&str>,
    head: i64,
    page_last: i64,
    has_more: bool,
    cursor: i64,
) -> PageOutcome {
    let step = pump_step(previous_journal, seen_journal, head, cursor);
    let next = match step {
        // A restarted journal's numbering says nothing about the old one, so
        // the head is the only safe place to resume. Subscribers are told to
        // re-read the state, which is what covers the gap.
        PumpStep::Restart => head.max(page_last).max(0),
        // `page_last` may go backwards only if the bridge is confused; never
        // let the cursor regress.
        PumpStep::Advance => page_last.max(cursor).max(0),
        PumpStep::Idle => cursor,
    };
    PageOutcome {
        step,
        cursor: next,
        more_pages: has_more && next > cursor,
    }
}

/// One shared `/events` long-poll loop feeding every `listen()` subscriber.
///
/// The cursor is a sequence number, and a sequence number only means anything
/// inside the journal that produced it: an NVDA restart begins again at zero,
/// so a cursor carried across one would suppress every notification until the
/// new sequence outgrew the old — minutes of silence, and no way to tell it
/// apart from an idle screen reader. The journal's id is what makes the reset
/// detectable, and a version that goes backwards is the same fact when the
/// bridge is too old to send an id.
fn spawn_event_pump(
    bridge_client: BridgeClient,
    subscription_tx: broadcast::Sender<i64>,
    shutdown: Arc<AtomicBool>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut cursor = 0_i64;
        let mut journal_id: Option<String> = None;
        // Catch-up pages are fetched back to back; only an empty poll goes back
        // to the long poll. A burst larger than one page must not cost a
        // notification per page plus a one-second wait between them.
        let mut catching_up = false;
        while !shutdown.load(Ordering::SeqCst) {
            let timeout_ms = if catching_up { 0 } else { 1000 };
            // Send the journal id with the numeric cursor. Without it the
            // bridge can only treat `since` as a version inside whatever
            // journal happens to be current; with it, a replacement is
            // reported as a mismatch instead of silently reading unrelated
            // history.
            let events_path = match journal_id.as_deref() {
                Some(id) => format!(
                    "/events?since={cursor}&timeout_ms={timeout_ms}&journal_id={id}"
                ),
                None => format!("/events?since={cursor}&timeout_ms={timeout_ms}"),
            };
            match bridge_client.get_json(&events_path).await {
                Ok(page) => {
                    // An explicit resync means the returned history cannot be
                    // treated as complete. Do not advance past it as if it
                    // were: jump to the bridge's typed resync cursor and tell
                    // subscribers to re-read the state.
                    if page
                        .get("resync_required")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    {
                        let resync = page.get("resync_from");
                        let next_cursor = resync
                            .and_then(|value| value.get("version"))
                            .and_then(Value::as_i64)
                            .unwrap_or(cursor);
                        if let Some(next_id) = resync
                            .and_then(|value| value.get("journal_id"))
                            .and_then(Value::as_str)
                            .filter(|id| !id.is_empty())
                        {
                            journal_id = Some(next_id.to_string());
                        }
                        warn!(
                            reason = ?page.get("resync_reason").and_then(|value| value.as_str()),
                            previous_cursor = cursor,
                            resync_cursor = next_cursor,
                            "Bridge event history was incomplete; resynchronising"
                        );
                        cursor = next_cursor.max(0);
                        let _ = subscription_tx.send(cursor);
                        catching_up = false;
                        continue;
                    }
                    let head = page
                        .get("event_version")
                        .or_else(|| page.get("state_version"))
                        .and_then(Value::as_i64)
                        .unwrap_or(cursor);
                    // Older bridges have no `to_version`, and only ever returned
                    // the newest page, so the head was the right cursor there.
                    let page_last = page
                        .get("to_version")
                        .and_then(Value::as_i64)
                        .unwrap_or(head);
                    let has_more = page.get("has_more").and_then(Value::as_bool).unwrap_or(false);
                    let seen_id = page
                        .get("journal_id")
                        .and_then(Value::as_str)
                        .filter(|id| !id.is_empty())
                        .map(str::to_string);
                    let outcome = consume_page(
                        journal_id.as_deref(),
                        seen_id.as_deref(),
                        head,
                        page_last,
                        has_more,
                        cursor,
                    );
                    match outcome.step {
                        PumpStep::Restart => {
                            warn!(
                                previous = ?journal_id,
                                current = ?seen_id,
                                previous_cursor = cursor,
                                new_head = head,
                                "Bridge event journal restarted; resetting the cursor"
                            );
                            // Subscribers have to re-read the state: the events
                            // across the restart are not in this journal.
                            let _ = subscription_tx.send(outcome.cursor);
                        }
                        PumpStep::Advance => {
                            let _ = subscription_tx.send(outcome.cursor);
                        }
                        PumpStep::Idle => {}
                    }
                    cursor = outcome.cursor;
                    catching_up = outcome.more_pages;
                    if seen_id.is_some() {
                        journal_id = seen_id;
                    }
                }
                Err(err) => {
                    catching_up = false;
                    warn!(error = %err, "Bridge event poll failed; retrying in 1s");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        }
    })
}

/// Periodically re-fetch `/tools` and swap in the catalog when `schema_hash`
/// changes. rmcp 3.9 keeps the tool capability static, so a swap on its own is
/// invisible to the client: `notifications/tools/list_changed` is what makes it
/// re-list, and the peer is only known after the handshake (see `on_initialized`).
fn spawn_catalog_refresh(
    bridge_client: BridgeClient,
    tool_catalog: Arc<RwLock<ToolCatalog>>,
    peer: Arc<RwLock<Option<Peer<RoleServer>>>>,
    shutdown: Arc<AtomicBool>,
    period: Duration,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(period);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        // The catalog was just fetched at startup; wait one full period first.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            if shutdown.load(Ordering::SeqCst) {
                break;
            }
            match bridge_client.fetch_tool_catalog().await {
                Ok(new_catalog) => {
                    let (schema_changed, instance_changed, previous_instance) = {
                        let current = tool_catalog
                            .read()
                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                        (
                            schema_hash_changed(
                                current.schema_hash.as_deref(),
                                new_catalog.schema_hash.as_deref(),
                            ),
                            bridge_replaced(
                                current.instance_id.as_deref(),
                                new_catalog.instance_id.as_deref(),
                            ),
                            current.instance_id.clone(),
                        )
                    };
                    let changed = schema_changed || instance_changed;
                    if changed {
                        let previous_hash = {
                            let mut current = tool_catalog
                                .write()
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                            let previous = current.schema_hash.clone();
                            *current = new_catalog;
                            previous
                        };
                        if instance_changed {
                            warn!(
                                previous_instance = ?previous_instance,
                                "Bridge instance changed; cached references and event cursors are no longer valid"
                            );
                        }
                        info!(
                            previous_hash = ?previous_hash,
                            "Bridge tool contract changed; swapped in refreshed catalog"
                        );
                        let peer = peer
                            .read()
                            .unwrap_or_else(|poisoned| poisoned.into_inner())
                            .clone();
                        match peer {
                            Some(peer) => {
                                let notification = ServerNotification::ToolListChangedNotification(
                                    ToolListChangedNotification {
                                        method: Default::default(),
                                        extensions: Default::default(),
                                    },
                                );
                                match peer.send_notification(notification).await {
                                    Ok(()) => info!(
                                        "Announced refreshed tool surface; client will re-list"
                                    ),
                                    Err(err) => warn!(
                                        error = %err,
                                        "Refresh happened but the client could not be notified"
                                    ),
                                }
                            }
                            None => info!(
                                "Catalog refreshed before the client handshake; the next tools/list sees it"
                            ),
                        }
                    }
                }
                Err(err) => {
                    warn!(error = %err, "Bridge catalog refresh failed; keeping current catalog");
                }
            }
        }
    })
}

fn lower_process_priority() {
    // Primary NVDA freeze prevention is via single-worker-thread Tokio runtime (see tokio::main macro).
    // Direct Windows API process priority calls require feature gating; the single-threaded
    // async executor is the most reliable cross-platform mitigation.
}

fn init_logging(log_level: &str) {
    let default_directive = match log_level.to_ascii_uppercase().as_str() {
        "TRACE" => "trace",
        "DEBUG" => "debug",
        "WARNING" => "warn",
        "ERROR" => "error",
        "CRITICAL" => "error",
        _ => "info",
    };

    let filter = EnvFilter::try_from_default_env()
        .or_else(|_| EnvFilter::try_new(default_directive))
        .unwrap_or_else(|_| EnvFilter::new("info"));

    let subscriber = fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .with_target(true)
        .finish();

    let _ = tracing::subscriber::set_global_default(subscriber);

    info!("NVDA MCP Rust server configured with: single-worker-thread Tokio runtime, BELOW_NORMAL process priority");
}

#[tokio::main(flavor="multi_thread", worker_threads=1)]
async fn main() -> Result<()> {
    let settings = Settings::from_env()?;
    init_logging(&settings.log_level);

    lower_process_priority();

    info!(bridge_url = %settings.bridge_url, "Initializing NVDA MCP Rust server");

    let bridge_client = BridgeClient::new(settings.bridge_url, settings.bridge_token)
        .context("Failed to initialize bridge client")?;

    if !bridge_client.health_check().await {
        warn!("Bridge health check failed - continuing to tool discovery");
    }

    let mut retries = 5;
    let mut catalog_opt = None;
    while retries > 0 {
        match bridge_client.fetch_tool_catalog().await {
            Ok(catalog) => {
                catalog_opt = Some(catalog);
                break;
            }
            Err(err) => {
                retries -= 1;
                if retries > 0 {
                    warn!(error = %err, remaining = retries, "Bridge not yet ready, retrying in 1s...");
                    tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
                } else {
                    warn!(
                        error = %err,
                        "Bridge still unreachable; serving an empty tool catalog and retrying in the background"
                    );
                }
            }
        }
    }
    let tool_catalog = catalog_opt.unwrap_or_else(|| ToolCatalog::empty("bridge unreachable"));

    if tool_catalog.tools_for_mcp.is_empty() {
        info!(
            "No tools available yet. The catalog refresh loop will install them as soon as the bridge answers."
        );
    } else {
        info!(
            tool_count = tool_catalog.tools_for_mcp.len(),
            bridge_version = %tool_catalog.bridge_version,
            schema_hash = ?tool_catalog.schema_hash,
            "Discovered tools from bridge"
        );
    }

    let server = NvdaServer::new(bridge_client.clone(), tool_catalog);

    let _event_pump = spawn_event_pump(
        bridge_client.clone(),
        server.subscription_tx.clone(),
        Arc::clone(&server.shutdown),
    );

    let _catalog_refresh = match catalog_refresh_period() {
        Some(period) => {
            info!(period_secs = period.as_secs(), "Catalog refresh enabled");
            Some(spawn_catalog_refresh(
                bridge_client.clone(),
                Arc::clone(&server.tool_catalog),
                Arc::clone(&server.peer),
                Arc::clone(&server.shutdown),
                period,
            ))
        }
        None => {
            info!("Catalog refresh disabled by NVDA_BRIDGE_CATALOG_REFRESH_SECS=0");
            None
        }
    };

    let transport = (tokio::io::stdin(), tokio::io::stdout());
    let service = server
        .serve(transport)
        .await
        .map_err(|err| anyhow!("Failed to start MCP stdio service: {err}"))?;

    info!("NVDA MCP Rust server ready on stdio");
    service
        .waiting()
        .await
        .map_err(|err| anyhow!("MCP service error: {err}"))?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::BridgeToolConstraints;

    #[test]
    fn parse_action_payload_success_uses_data() {
        let payload = json!({
            "success": true,
            "data": {"message": "ok"}
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        assert!(parsed.success);
        assert_eq!(parsed.data.unwrap()["message"], "ok");
    }

    #[test]
    fn parse_action_payload_supports_legacy_result() {
        let payload = json!({
            "success": true,
            "result": {"legacy": true}
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        assert!(parsed.success);
        assert_eq!(parsed.data.unwrap()["legacy"], true);
    }

    #[test]
    fn parse_action_payload_error_shape_string_code() {
        let payload = json!({
            "success": false,
            "request_id": "test-123",
            "error": {
                "code": "INVALID_PARAMS",
                "message": "direction required"
            }
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        assert!(!parsed.success);
        assert_eq!(parsed.request_id.as_deref(), Some("test-123"));
        let err = parsed.error.unwrap();
        assert_eq!(err.code.as_str(), "INVALID_PARAMS");
        assert_eq!(err.message, "direction required");
    }

    #[test]
    fn parse_action_payload_error_shape_int_code() {
        let payload = json!({
            "success": false,
            "request_id": "test-456",
            "action": "get_window_title",
            "duration_ms": 12.5,
            "error": {
                "code": 4102,
                "name": "INVALID_ACTION_NAME",
                "message": "Unknown action",
                "category": "validation",
                "retry_policy": "never"
            }
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        assert!(!parsed.success);
        assert_eq!(parsed.request_id.as_deref(), Some("test-456"));
        assert_eq!(parsed.action.as_deref(), Some("get_window_title"));
        assert!((parsed.duration_ms.unwrap() - 12.5).abs() < 0.01);
        let err = parsed.error.unwrap();
        assert_eq!(err.code.as_str(), "4102");
        assert_eq!(err.name.as_deref(), Some("INVALID_ACTION_NAME"));
        assert_eq!(err.category.as_deref(), Some("validation"));
        assert_eq!(err.retry_policy.as_deref(), Some("never"));
    }

    #[test]
    fn parse_action_payload_keeps_the_recovery_object() {
        let payload = json!({
            "success": false,
            "error": {
                "code": 4206,
                "name": "STALE_REFERENCE",
                "message": "stale",
                "recovery": {"action": "observe", "hint": "Call observe again."}
            }
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        let error = parsed.error.unwrap();
        assert_eq!(error.recovery.unwrap()["action"], "observe");
    }

    #[test]
    fn annotation_marks_dangerous_tool() {
        let tool = BridgeToolDef {
            name: "execute_python".to_string(),
            description: "x".to_string(),
            input_schema: serde_json::from_value(json!({"type":"object"})).unwrap(),
            output_type: Some("PythonExecutionResult".to_string()),
            version: "1.0.0".to_string(),
            category: "developer".to_string(),
            constraints: Some(BridgeToolConstraints {
                dangerous: Some(true),
                requires_explicit_opt_in: Some(true),
                ..Default::default()
            }),
            destructive: Some(true),
            read_only: Some(false),
            idempotent: Some(false),
            ..Default::default()
        };

        let ann = annotation_for_tool(&tool);
        assert_eq!(ann.read_only_hint, Some(false));
        assert_eq!(ann.destructive_hint, Some(true));
        assert_eq!(ann.idempotent_hint, Some(false));
    }

    #[test]
    fn annotation_uses_explicit_metadata() {
        let tool = BridgeToolDef {
            name: "custom_mutation".to_string(),
            description: "mutation".to_string(),
            input_schema: serde_json::from_value(json!({"type":"object"})).unwrap(),
            output_type: None,
            version: "1.0.0".to_string(),
            category: "custom".to_string(),
            read_only: Some(false),
            destructive: Some(false),
            idempotent: Some(true),
            ..Default::default()
        };

        let ann = annotation_for_tool(&tool);
        assert_eq!(ann.read_only_hint, Some(false));
        assert_eq!(ann.destructive_hint, Some(false));
        assert_eq!(ann.idempotent_hint, Some(true));
    }

    #[test]
    fn resolve_bridge_token_rejects_insecure_default() {
        unsafe {
            env::set_var("NVDA_BRIDGE_TOKEN", "nvda-bridge-secret-change-me");
            env::remove_var("ALLOW_INSECURE_DEFAULT_TOKEN");
        }
        let result = resolve_bridge_token();
        assert!(result.is_err(), "Must reject insecure default token by default");
        assert!(result.unwrap_err().to_string().contains("Insecure default bridge token"));

        unsafe {
            env::set_var("ALLOW_INSECURE_DEFAULT_TOKEN", "1");
        }
        let result_allowed = resolve_bridge_token();
        assert!(result_allowed.is_ok(), "Must allow default token when migration flag set");
        assert_eq!(result_allowed.unwrap(), "nvda-bridge-secret-change-me");

        // Clean up
        unsafe {
            env::remove_var("NVDA_BRIDGE_TOKEN");
            env::remove_var("ALLOW_INSECURE_DEFAULT_TOKEN");
        }
    }

    #[test]
    fn resolve_bridge_token_supports_token_path_and_config_path() {
        let temp_dir = std::env::temp_dir().join(format!("nvda_test_{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()));
        std::fs::create_dir_all(&temp_dir).unwrap();
        let key_file = temp_dir.join("token.key");
        std::fs::write(&key_file, "custom-discovered-token-12345").unwrap();

        unsafe {
            env::remove_var("NVDA_BRIDGE_TOKEN");
            env::set_var("NVDA_TOKEN_PATH", key_file.to_str().unwrap());
        }

        let result = resolve_bridge_token().unwrap();
        assert_eq!(result, "custom-discovered-token-12345");

        unsafe {
            env::remove_var("NVDA_TOKEN_PATH");
        }
        let _ = std::fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn next_request_id_is_non_empty_and_prefixed() {
        let id = BridgeClient::next_request_id();
        assert!(!id.is_empty());
        assert!(id.starts_with("mcp-"));
    }

    #[test]
    fn validate_output_accepts_inner_data_schema() {
        let runtime = ToolRuntime {
            definition: BridgeToolDef {
                name: "get_window_title".to_string(),
                description: "x".to_string(),
                input_schema: serde_json::from_value(json!({"type":"object"})).unwrap(),
                output_type: Some("WindowInfo".to_string()),
                version: "1.0.0".to_string(),
                category: "window".to_string(),
                constraints: None,
                ..Default::default()
            },
            input_schema_value: json!({"type":"object"}),
            data_schema_value: None,
            output_schema_value: Some(json!({
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"}
                        },
                        "required": ["title"],
                        "additionalProperties": false
                    }
                },
                "required": ["success", "data"],
                "additionalProperties": false
            })),
        };

        // Payload with extra envelope fields — realistic HTTP response shape.
        // The new validate_output should match the inner `data` schema and ignore
        // transport fields like request_id, action, duration_ms, bridge, etc.
        let payload = BridgeActionEnvelope {
            success: true,
            request_id: Some("req-1".into()),
            action: Some("get_window_title".into()),
            duration_ms: Some(5.0),
            bridge: Some(BridgeMeta {
                bridge_version: Some("1.0.0".into()),
                api_level: Some(1),
                instance_id: Some("instance-1".into()),
                started_at: Some("2026-01-01T00:00:00+00:00".into()),
            }),
            nvda_status_snapshot: Some(NvdaStatusSnapshot {
                secure_screen: Some(false),
                modal_blocking: Some(false),
                menu_open: Some(false),
            }),
            data: Some(json!({"title":"Notepad"})),
            error: None,
            result: None,
            ..Default::default()
        };

        assert!(
            NvdaServer::validate_output(&runtime, &payload).is_ok(),
            "validate_output should pass: inner data matches schema, envelope extras ignored"
        );
    }

    #[test]
    fn schema_json_loads_and_builds_catalog() {
        // Load schema.json from repo root (one level up from this crate)
        let schema_path = std::path::Path::new("../schema.json");
        let text = std::fs::read_to_string(schema_path)
            .expect("Failed to read ../schema.json - ensure tests run from crate root");

        let payload: BridgeToolsResponse = serde_json::from_str(&text)
            .expect("schema.json did not deserialize into BridgeToolsResponse");

        // Validate payload shape as BridgeClient expects
        validate_tools_payload(
            &payload.protocol,
            &payload.protocol_version,
            &payload.bridge_version,
            payload.api_level,
            payload.supported_api_levels.as_deref().unwrap_or(&[]),
            payload.count,
            payload.tools.len(),
        )
        .expect("validate_tools_payload failed");

        // Ensure build_catalog can consume it and produce a catalog
        let mut catalog = BridgeClient::build_catalog(payload).expect("build_catalog failed");

        assert!(catalog.tools_for_mcp.len() > 0, "No tools found in schema.json");
        assert_eq!(catalog.tools_for_mcp.len(), catalog.runtime_by_name.len(), "Mismatch between tools_for_mcp and runtime_by_name sizes");
        let find_tools = catalog
            .tools_for_mcp
            .iter()
            .find(|tool| tool.name == "find_tools")
            .expect("find_tools missing from catalog");
        assert_eq!(
            find_tools.title.as_deref(),
            Some("Discovery · Find Tools"),
            "tool titles should group by category without renaming the tool"
        );
        assert!(
            find_tools.output_schema.is_some(),
            "catalog construction keeps output schemas for validation"
        );

        // Compact mode strips only the advertised copy. Runtime validation and
        // get_tool_schema(include_output_schema=true) still have the schema.
        catalog.omit_output_schemas();
        assert!(
            catalog.tools_for_mcp.iter().all(|tool| tool.output_schema.is_none()),
            "compact mode must not advertise output schemas"
        );
        assert!(
            catalog
                .runtime_by_name
                .get("find_tools")
                .and_then(|runtime| runtime.output_schema_value.as_ref())
                .is_some(),
            "runtime must keep the output schema used to validate responses"
        );
    }

    #[tokio::test]
    async fn httpmock_fetch_tools_and_execute_action() -> Result<()> {
        use httpmock::prelude::*;

        // Start mock server
        let server = MockServer::start();

        // Serve schema.json at /tools
        let schema_text = std::fs::read_to_string(std::path::Path::new("../schema.json"))
            .expect("Failed to read ../schema.json");

        let _tools_mock = server.mock(|when, then| {
            when.method(GET).path("/tools");
            then.status(200)
                .header("content-type", "application/json")
                .body(schema_text.clone());
        });

        // Respond to action POSTs
        let _action_mock = server.mock(|when, then| {
            when.method(POST).path("/action");
            then.status(200)
                .header("content-type", "application/json")
                .body(r#"{"success":true,"data":{"title":"MockWindow"}}"#);
        });

        // Create BridgeClient pointed at mock server
        let base = server.url("");
        let base_url = base.trim_end_matches('/').to_string();
        let bridge_client = BridgeClient::new(base_url, "test-token".to_string())?;

        // Fetch tool catalog
        let catalog = bridge_client.fetch_tool_catalog().await?;
        assert!(catalog.tools_for_mcp.len() > 0);

        // Execute an action and validate response
        let params: serde_json::Map<String, Value> = serde_json::Map::new();
        let result = bridge_client.execute_action("get_window_title", params).await?;
        assert!(result.success);
        let data = result.data.expect("Expected data in action response");
        assert_eq!(data.get("title").and_then(Value::as_str).unwrap_or(""), "MockWindow");

        Ok(())
    }

    #[tokio::test]
    async fn wait_for_speech_request_allows_its_full_bridge_wait() -> Result<()> {
        use httpmock::prelude::*;

        let server = MockServer::start();
        let action_mock = server.mock(|when, then| {
            when.method(POST).path("/action");
            then.status(200)
                .header("content-type", "application/json")
                .delay(Duration::from_millis(200))
                .body(r#"{"success":true,"data":{"timed_out":true}}"#);
        });
        let mut bridge_client = BridgeClient::new(server.url(""), "test-token".to_string())?;
        bridge_client.client = Client::builder()
            .timeout(Duration::from_millis(50))
            .build()?;
        let params: Map<String, Value> = serde_json::from_value(json!({"timeout_ms": 30000}))?;

        let response = bridge_client.execute_action("wait_for_speech", params).await?;

        assert!(response.success);
        assert_eq!(response.data.unwrap().get("timed_out"), Some(&json!(true)));
        action_mock.assert();
        Ok(())
    }

    #[test]
    fn server_capabilities_advertise_refreshable_tool_catalog() {
        let dummy_catalog = ToolCatalog {
            tools_for_mcp: Vec::new(),
            runtime_by_name: HashMap::new(),
            bridge_version: "1.0.0".into(),
            instance_id: None,
            schema_hash: None,
        };
        let dummy_client = BridgeClient::new("http://127.0.0.1:8765".into(), "dummy".into()).unwrap();
        let server = NvdaServer::new(dummy_client, dummy_catalog);
        let info = server.get_info();

        let tools_cap = info.capabilities.tools.expect("tools capability must be present");
        // The refresh loop swaps in a new surface when the add-on's schema hash
        // changes and then sends `notifications/tools/list_changed`. Clients only
        // honour that notification when the capability is advertised, and a
        // client that never re-lists keeps the tool set it saw at connect time.
        assert_eq!(tools_cap.list_changed, Some(true));

        // Resources back the live NVDA state surface, and subscriptions are
        // how clients learn that state changed.
        let resources_cap = info
            .capabilities
            .resources
            .expect("resources capability must be present");
        assert_ne!(resources_cap.subscribe, Some(false));
    }

    #[test]
    fn build_catalog_falls_back_to_per_tool_output_schema() {
        let output_schema: Map<String, Value> = serde_json::from_value(json!({
            "type": "object",
            "properties": {
                "data": {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            }
        }))
        .unwrap();

        let payload = BridgeToolsResponse {
            protocol: PROTOCOL_NAME.to_string(),
            protocol_version: "1.0.0".to_string(),
            bridge_version: "1.0.0".to_string(),
            api_level: API_LEVEL,
            supported_api_levels: Some(vec![API_LEVEL]),
            instance_id: Some("instance-1".to_string()),
            started_at: Some("2026-01-01T00:00:00+00:00".to_string()),
            // Deliberately empty: the output type is not in the types map.
            types: Map::new(),
            tools: vec![BridgeToolDef {
                name: "probe".to_string(),
                description: "probe".to_string(),
                input_schema: serde_json::from_value(json!({"type": "object"})).unwrap(),
                output_type: Some("MissingType".to_string()),
                output_schema: Some(output_schema),
                version: "1.0.0".to_string(),
                category: "diagnostics".to_string(),
                ..Default::default()
            }],
            count: 1,
            schema_hash: Some("hash-1".to_string()),
        };

        let catalog = BridgeClient::build_catalog(payload).expect("build_catalog failed");
        let runtime = catalog.runtime_by_name.get("probe").expect("probe missing");
        assert!(
            runtime.output_schema_value.is_some(),
            "output_schema must be used when the types map lacks outputType"
        );
        assert_eq!(catalog.schema_hash.as_deref(), Some("hash-1"));
    }

    #[test]
    fn schema_hash_change_detection() {
        assert!(!schema_hash_changed(None, None));
        assert!(!schema_hash_changed(Some("a"), Some("a")));
        assert!(schema_hash_changed(Some("a"), Some("b")));
        assert!(schema_hash_changed(None, Some("a")));
        assert!(schema_hash_changed(Some("a"), None));
    }

    #[test]
    fn pump_advances_within_one_journal() {
        assert_eq!(pump_step(Some("a"), Some("a"), 5, 4), PumpStep::Advance);
        assert_eq!(pump_step(Some("a"), Some("a"), 4, 4), PumpStep::Idle);
    }

    #[test]
    fn pump_resets_when_the_journal_is_replaced() {
        // NVDA restarted: its journal begins at zero, and the old cursor is
        // meaningless. Without the id this page would look idle.
        assert_eq!(pump_step(Some("a"), Some("b"), 0, 900), PumpStep::Restart);
    }

    #[test]
    fn pump_resets_when_the_version_goes_backwards() {
        // A bridge too old to send journal_id still has to be caught.
        assert_eq!(pump_step(None, None, 3, 900), PumpStep::Restart);
        assert_eq!(pump_step(Some("a"), Some("a"), 3, 900), PumpStep::Restart);
    }

    #[test]
    fn pump_does_not_reset_on_the_first_page() {
        assert_eq!(pump_step(None, Some("a"), 7, 0), PumpStep::Advance);
        assert_eq!(pump_step(None, Some("a"), 0, 0), PumpStep::Idle);
    }

    #[test]
    fn a_bridge_replacement_is_detected_by_instance_id() {
        assert!(bridge_replaced(Some("old"), Some("new")));
        assert!(!bridge_replaced(Some("same"), Some("same")));
        // Unknown is not continuity: a catalog that cannot identify its bridge
        // must not be assumed to be the same process.
        assert!(bridge_replaced(None, Some("new")));
        assert!(bridge_replaced(Some("old"), None));
        assert!(!bridge_replaced(None, None));
    }

    #[test]
    fn one_page_advances_to_the_last_event_delivered() {
        // Head 5, one page holding everything: the cursor lands on 5.
        let outcome = consume_page(Some("a"), Some("a"), 5, 5, false, 0);
        assert_eq!(outcome.step, PumpStep::Advance);
        assert_eq!(outcome.cursor, 5);
        assert!(!outcome.more_pages);
    }

    #[test]
    fn a_full_page_advances_to_the_page_not_the_head() {
        // 250 events, 100 per page. The old code set the cursor to the head
        // (250) and skipped events 101-250 with no record that it had.
        let outcome = consume_page(Some("a"), Some("a"), 250, 100, true, 0);
        assert_eq!(outcome.step, PumpStep::Advance);
        assert_eq!(outcome.cursor, 100, "cursor must match the last consumed event");
        assert!(outcome.more_pages, "a partial page means another is due");
    }

    #[test]
    fn catch_up_walks_every_page_and_stops_at_the_head() {
        let mut cursor = 0;
        let mut seen = Vec::new();
        for page_last in [100, 200, 250] {
            let has_more = page_last != 250;
            let outcome = consume_page(Some("a"), Some("a"), 250, page_last, has_more, cursor);
            seen.push((cursor, outcome.cursor, outcome.more_pages));
            cursor = outcome.cursor;
        }
        assert_eq!(
            seen,
            vec![(0, 100, true), (100, 200, true), (200, 250, false)]
        );
        assert_eq!(cursor, 250);
    }

    #[test]
    fn events_arriving_during_catch_up_are_picked_up_by_the_next_page() {
        // The head grew between pages; only the page's own last event advances
        // the cursor, so the extra events are still ahead of it.
        let outcome = consume_page(Some("a"), Some("a"), 320, 200, true, 100);
        assert_eq!(outcome.cursor, 200);
        assert!(outcome.more_pages);
    }

    #[test]
    fn has_more_without_progress_does_not_spin() {
        // A bridge that reports more pages but keeps returning the same page
        // would otherwise be polled at full speed forever.
        let outcome = consume_page(Some("a"), Some("a"), 7, 7, true, 7);
        assert_eq!(outcome.cursor, 7);
        assert!(!outcome.more_pages);
    }

    #[test]
    fn a_restarted_journal_resumes_at_its_head() {
        let outcome = consume_page(Some("a"), Some("b"), 3, 3, true, 900);
        assert_eq!(outcome.step, PumpStep::Restart);
        assert_eq!(outcome.cursor, 3);
        assert!(!outcome.more_pages);
    }

    #[test]
    fn a_stale_cursor_never_regresses() {
        let outcome = consume_page(None, None, 3, 3, false, 900);
        assert_eq!(outcome.step, PumpStep::Restart);
        assert_eq!(outcome.cursor, 3);
    }

    #[test]
    fn a_bridge_without_to_version_falls_back_to_the_head() {
        // Old bridges returned the newest page, so the head is what they meant.
        // `page_last` is passed as the head by the pump in that case.
        let outcome = consume_page(Some("a"), Some("a"), 42, 42, false, 0);
        assert_eq!(outcome.cursor, 42);
    }

    #[test]
    fn api_level_is_accepted_when_the_bridge_lists_it() {
        // A newer bridge that still serves this companion's level.
        let accepted = validate_tools_payload(
            PROTOCOL_NAME, "1.0.0", "1.0.0", API_LEVEL + 1, &[2, 3, API_LEVEL], 1, 1,
        );
        assert!(accepted.is_ok(), "{accepted:?}");

        // The same level, announced as its own.
        let exact = validate_tools_payload(
            PROTOCOL_NAME, "1.0.0", "1.0.0", API_LEVEL, &[], 1, 1,
        );
        assert!(exact.is_ok(), "{exact:?}");
    }

    #[test]
    fn api_level_is_refused_when_the_bridge_cannot_serve_it() {
        let refused = validate_tools_payload(
            PROTOCOL_NAME, "1.0.0", "1.0.0", 3, &[2, 3], 1, 1,
        );
        let message = refused.expect_err("a bridge serving only levels 2 and 3 cannot satisfy level 1");
        assert!(message.contains("does not support"), "{message}");
    }

    #[test]
    fn success_content_keeps_legacy_metadata_aliases_without_collisions() {
        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"sent": true})),
            result: Some(json!({"ok": true, "state_version": 7, "changed": true})),
            trace_id: Some("companion-trace".into()),
            ..Default::default()
        };

        let result = NvdaServer::success_result(&payload);
        let structured = result.structured_content.expect("structured content");
        assert_eq!(structured["sent"], true);
        assert_eq!(structured["result"]["state_version"], 7);
        assert_eq!(structured["trace_id"], "companion-trace");
        assert_eq!(structured["_meta"]["result"]["state_version"], 7);
        assert_eq!(structured["_meta"]["trace_id"], "companion-trace");
        let text = first_text(&result.content);
        let same: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(same, structured);
    }

    #[test]
    fn success_content_keeps_tool_result_and_input_trace_id() {
        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"result": "the executed string", "trace_id": "requested-trace"})),
            result: Some(json!({"ok": true})),
            trace_id: Some("companion-trace".into()),
            ..Default::default()
        };

        let result = NvdaServer::success_result(&payload);
        let structured = result.structured_content.expect("structured content");

        assert_eq!(structured["result"], "the executed string");
        assert_eq!(structured["trace_id"], "requested-trace");
        assert_eq!(structured["_meta"]["result"]["ok"], true);
        assert_eq!(structured["_meta"]["trace_id"], "companion-trace");
    }

    #[test]
    fn output_schema_keeps_flat_data_and_adds_companion_metadata() {
        let schema = json!({
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": false
        });
        let extended = extend_output_schema(&schema);
        assert_eq!(extended["properties"]["title"]["type"], "string");
        assert_eq!(extended["properties"]["result"], json!({}));
        assert_eq!(extended["properties"]["trace_id"]["type"], "string");
        assert!(extended["properties"]["_meta"]["properties"]["result"].is_object());
        assert_eq!(
            extended["properties"]["_meta"]["properties"]["trace_id"]["type"],
            "string"
        );
        assert_eq!(extended["required"], json!(["title", "_meta"]));
        assert_eq!(extended["additionalProperties"], false);

        let collision_schema = json!({
            "type": "object",
            "properties": {
                "result": {"type": "string"},
                "trace_id": {"type": "string"}
            },
            "additionalProperties": false
        });
        let extended_collision_schema = extend_output_schema(&collision_schema);
        assert_eq!(
            extended_collision_schema["properties"]["result"]["type"],
            "string"
        );
        assert_eq!(
            extended_collision_schema["properties"]["trace_id"]["type"],
            "string"
        );
        assert!(extended_collision_schema["properties"]["_meta"].is_object());
    }

    fn first_text(blocks: &[ContentBlock]) -> String {
        match blocks.first() {
            Some(ContentBlock::Text(text)) => text.text.clone(),
            _ => panic!("expected a text block"),
        }
    }

    #[test]
    fn a_parsed_envelope_keeps_the_trace_identifier() {
        let payload = json!({
            "success": true,
            "action": "press",
            "data": {"sent": true},
            "trace_id": "at-abc",
            "trace": {"trace_id": "at-abc", "outcome": "succeeded"},
        });

        let envelope = BridgeClient::parse_action_payload(payload).unwrap();

        assert_eq!(envelope.trace_id.as_deref(), Some("at-abc"));
        assert_eq!(envelope.trace.unwrap()["outcome"], "succeeded");
    }

    #[test]
    fn a_failed_envelope_keeps_the_trace_identifier_too() {
        let payload = json!({
            "success": false,
            "action": "press",
            "error": {"code": 4103, "message": "bad params"},
            "trace_id": "at-def",
            "trace": {"outcome": "failed"},
        });

        let envelope = BridgeClient::parse_action_payload(payload).unwrap();

        assert!(!envelope.success);
        assert_eq!(envelope.trace_id.as_deref(), Some("at-def"));
        assert_eq!(envelope.trace.unwrap()["outcome"], "failed");
    }

    #[test]
    fn success_content_keeps_trace_id_without_the_verbose_trace() {
        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"sent": true})),
            trace_id: Some("at-abc".into()),
            trace: Some(json!({"trace_id": "at-abc", "outcome": "succeeded"})),
            ..Default::default()
        };

        let result = NvdaServer::success_result(&payload);
        let text = first_text(&result.content);
        let envelope: Value = serde_json::from_str(&text).unwrap();

        assert_eq!(envelope["trace_id"], "at-abc");
        assert_eq!(envelope["_meta"]["trace_id"], "at-abc");
        // The full trace is opt-in via get_action_trace; it must not ride along
        // on every call.
        assert!(envelope["trace"].is_null());
        assert_eq!(result.structured_content.unwrap()["sent"], true);
    }

    #[test]
    fn error_content_is_parseable_json_without_structured_content() {
        let payload = BridgeActionEnvelope {
            success: false,
            error: Some(BridgeError {
                code: ErrorCode("4103".into()),
                message: "bad params".into(),
                ..Default::default()
            }),
            trace_id: Some("at-def".into()),
            trace: Some(json!({"outcome": "failed"})),
            ..Default::default()
        };

        let result = NvdaServer::error_result(&payload);
        assert!(result.is_error.unwrap_or(false));
        assert!(result.structured_content.is_none());
        let detail: Value = serde_json::from_str(&first_text(&result.content)).unwrap();
        assert_eq!(detail["trace_id"], "at-def");
        assert_eq!(detail["error"]["code"], "4103");
        assert!(detail["trace"].is_null());
    }

    #[test]
    fn a_tool_error_keeps_the_typed_recovery_object() {
        let payload = BridgeActionEnvelope {
            success: false,
            error: Some(BridgeError {
                code: ErrorCode("4206".into()),
                message: "stale".into(),
                name: Some("STALE_REFERENCE".into()),
                category: Some("nvda_state".into()),
                retry_policy: Some("after_state_change".into()),
                details: Some(json!({"reason_code": "window", "ref": "f"})),
                recovery: Some(json!({"action": "observe", "hint": "Call observe again."})),
                ..Default::default()
            }),
            ..Default::default()
        };

        let result = NvdaServer::error_result(&payload);
        let text: Value = serde_json::from_str(&first_text(&result.content)).unwrap();
        assert_eq!(text["error"]["name"], "STALE_REFERENCE");
        assert_eq!(text["error"]["details"]["reason_code"], "window");
        assert_eq!(text["error"]["recovery"]["action"], "observe");
    }

    #[test]
    fn input_validation_names_the_offending_field() {
        let runtime = ToolRuntime {
            definition: BridgeToolDef {
                name: "observe".to_string(),
                ..Default::default()
            },
            input_schema_value: json!({
                "type": "object",
                "properties": {"max_text": {"type": "integer", "maximum": 2000}},
                "additionalProperties": false
            }),
            output_schema_value: None,
            data_schema_value: None,
        };
        let params: Map<String, Value> = serde_json::from_value(json!({"max_text": 9999})).unwrap();
        let errors = NvdaServer::validate_input(&runtime, &params).expect_err("too large");
        assert!(errors.contains_key("/max_text"), "{errors:?}");

        let result = NvdaServer::input_error_result("observe", errors);
        let detail: Value = serde_json::from_str(&first_text(&result.content)).unwrap();
        assert_eq!(detail["error"]["code"], "4103");
        assert_eq!(detail["error"]["recovery"]["action"], "fix_arguments");
        assert!(detail["error"]["details"]["field_errors"]["/max_text"].is_string());
    }

    #[test]
    fn an_empty_catalog_is_served_instead_of_exiting() {
        let catalog = ToolCatalog::empty("bridge unreachable");
        assert!(catalog.tools_for_mcp.is_empty());
        assert!(catalog.bridge_version.starts_with("unavailable"));
        assert!(schema_hash_changed(catalog.schema_hash.as_deref(), Some("abc")));
    }

    fn named_tools(count: usize) -> Vec<Tool> {
        (0..count)
            .map(|index| Tool::new(format!("tool_{index}"), "x", Map::new()))
            .collect()
    }

    #[test]
    fn the_default_page_holds_every_tool() {
        let (page, next) = tools_page(named_tools(5), None, 100).unwrap();
        assert_eq!(page.len(), 5);
        assert!(next.is_none(), "a short page has no successor");
    }

    #[test]
    fn a_full_page_reports_an_opaque_cursor_and_resumes() {
        let (first, cursor) = tools_page(named_tools(5), None, 2).unwrap();
        assert_eq!(
            first.iter().map(|tool| tool.name.clone()).collect::<Vec<_>>(),
            ["tool_0", "tool_1"]
        );
        let cursor = cursor.expect("more tools remain");
        let (second, cursor) = tools_page(named_tools(5), Some(&cursor), 2).unwrap();
        assert_eq!(
            second.iter().map(|tool| tool.name.clone()).collect::<Vec<_>>(),
            ["tool_2", "tool_3"]
        );
        let (last, cursor) = tools_page(named_tools(5), cursor.as_deref(), 2).unwrap();
        assert_eq!(last[0].name, "tool_4");
        assert!(cursor.is_none());
    }

    #[test]
    fn an_unknown_tools_cursor_is_a_client_error() {
        for cursor in ["banana", "t", "tzz", "tffffffffffffffff"] {
            let error = tools_page(named_tools(3), Some(cursor), 2)
                .expect_err("malformed or out-of-range cursors must be rejected");
            assert_eq!(error.code.0, -32602, "invalid params code");
            assert!(
                error.message.contains("cursor"),
                "error should name the cursor: {}",
                error.message
            );
        }
    }
}
