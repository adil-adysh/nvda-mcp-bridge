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
    API_LEVEL, PROTOCOL_NAME, SUPPORTED_API_LEVELS, TOKEN_HEADER,
    BridgeActionEnvelope, BridgeError, BridgeMeta,
    BridgeToolDef, BridgeToolsResponse,
    ErrorCode, NvdaStatusSnapshot,
    validate_tools_payload,
};

static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(1);

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

#[derive(Debug, Clone)]
struct ToolRuntime {
    definition: BridgeToolDef,
    input_schema_value: Value,
    output_schema_value: Option<Value>,
}

#[derive(Debug)]
struct ToolCatalog {
    bridge_version: String,
    schema_hash: Option<String>,
    tools_for_mcp: Vec<Tool>,
    runtime_by_name: HashMap<String, ToolRuntime>,
}

#[derive(Debug, Clone)]
struct BridgeClient {
    client: Client,
    base_url: String,
    auth_token: String,
    /// Category allow-list for the catalog, from `NVDA_BRIDGE_CATEGORIES`.
    ///
    /// The catalog is context: every tool's schema is paid for by the agent on
    /// every turn. A companion serving a production agent can ask the bridge for
    /// actions and observations only, and never load the diagnostics.
    categories: Option<String>,
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

        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(timeout_secs))
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

        Ok(Self {
            client,
            base_url,
            auth_token,
            categories,
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
        Self::build_catalog(payload)
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

            let mcp_tool = Tool::new(
                def.name.clone(),
                format!("{} [category={}, version={}]", def.description, def.category, def.version),
                def.input_schema.clone(),
            )
            .annotate(annotation_for_tool(&def));


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
                    output_schema_value,
                },
            );
        }

        Ok(ToolCatalog {
            bridge_version: payload.bridge_version,
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
        let request_id = Self::next_request_id();
        let payload = json!({
            "request_id": request_id,
            "action": action,
            "params": params,
        });

        let url = format!("{}/action", self.base_url);
        let response = self
            .client
            .post(&url)
            .header(TOKEN_HEADER, &self.auth_token)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .json(&payload)
            .send()
            .await
            .with_context(|| format!("Failed to connect to bridge for action: {action}"))?;

        Self::parse_action_response(action, response).await
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
        Some(BridgeError {
            code,
            message,
            name,
            category,
            retry_policy,
            retry_after_ms,
            details,
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
}

impl NvdaServer {
    fn new(bridge_client: BridgeClient, tool_catalog: ToolCatalog) -> Self {
        Self::with_catalog(bridge_client, Arc::new(RwLock::new(tool_catalog)))
    }

    fn with_catalog(bridge_client: BridgeClient, tool_catalog: Arc<RwLock<ToolCatalog>>) -> Self {
        let (subscription_tx, _) = broadcast::channel(64);
        Self {
            bridge_client,
            tool_catalog,
            peer: Arc::new(RwLock::new(None)),
            subscription_tx,
            shutdown: Arc::new(AtomicBool::new(false)),
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

    fn validate_input(runtime: &ToolRuntime, params: &Map<String, Value>) -> Result<()> {
        let args_value = Value::Object(params.clone());
        if !jsonschema::is_valid(&runtime.input_schema_value, &args_value) {
            bail!(
                "Input validation failed for '{}': request arguments do not match inputSchema",
                runtime.definition.name
            );
        }
        Ok(())
    }

    /// Validate the `data` portion of a bridge response against the inner output schema.
    ///
    /// The output type schemas in `schema.json` define `{success, data: {...}, error}` envelopes,
    /// while the actual HTTP response also carries transport metadata (`request_id`, `action`,
    /// `duration_ms`, `bridge`, `nvda_status_snapshot`).  We validate only the `data` field against
    /// the inner schema to avoid false rejections from extra envelope fields.
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

        // Extract the inner `data` schema from the output type envelope
        // schema: `{success, data: {...}, error}` → `{...}` (the data schema).
        let inner_schema = output_schema_value
            .as_object()
            .and_then(|obj| obj.get("properties"))
            .and_then(|props| props.as_object())
            .and_then(|props| props.get("data"))
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

    /// Build MCP content from a successful bridge response.
    /// Includes protocol metadata (`request_id`, `action`, `duration_ms`, `bridge`,
    /// `nvda_status_snapshot`) for downstream AI agent observability and traceability.
    fn success_content(payload: &BridgeActionEnvelope) -> Vec<ContentBlock> {
        let mut envelope = json!({
            "success": true,
        });

        if let Some(ref data) = payload.data {
            envelope["data"] = data.clone();
        } else {
            envelope["data"] = Value::Object(Map::new());
        }

        // The uniform result block (§4.1) is additive: keep it alongside `data`.
        if let Some(ref result) = payload.result {
            envelope["result"] = result.clone();
        }

        // Diagnostic correlation: an agent needs the trace id to ask what
        // happened, and the summary to decide whether it needs to.
        if let Some(ref trace_id) = payload.trace_id {
            envelope["trace_id"] = json!(trace_id);
        }
        if let Some(ref trace) = payload.trace {
            envelope["trace"] = trace.clone();
        }

        // Attach protocol metadata for AI agents
        if let Some(ref rid) = payload.request_id {
            envelope["request_id"] = json!(rid);
        }
        if let Some(ref act) = payload.action {
            envelope["action"] = json!(act);
        }
        if let Some(dur) = payload.duration_ms {
            envelope["duration_ms"] = json!(dur);
        }
        if let Some(ref b) = payload.bridge {
            if let Ok(v) = serde_json::to_value(b) {
                envelope["bridge"] = v;
            }
        }
        if let Some(ref s) = payload.nvda_status_snapshot {
            if let Ok(v) = serde_json::to_value(s) {
                envelope["nvda_status_snapshot"] = v;
            }
        }

        vec![ContentBlock::text(serde_json::to_string_pretty(&envelope).unwrap_or_default())]
    }

    /// Build MCP content from a failed bridge response.
    /// Includes protocol metadata (`request_id`, `action`, `duration_ms`, `bridge`,
    /// `nvda_status_snapshot`) for downstream AI agent observability and traceability.
    fn error_content(payload: &BridgeActionEnvelope) -> Vec<ContentBlock> {
        let err = payload.error.clone().unwrap_or(BridgeError {
            code: ErrorCode("BRIDGE_ERROR".into()),
            message: "Unknown bridge error".into(),
            name: None,
            category: None,
            retry_policy: None,
            retry_after_ms: None,
            details: None,
        });

        let code_display = err.code.as_str();
        let summary = if let Some(ref name) = err.name {
            format!("Bridge tool error [{}] {}: {}", code_display, name, err.message)
        } else {
            format!("Bridge tool error {}: {}", code_display, err.message)
        };

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
        if let Some(ref d) = payload.data {
            detail["data"] = d.clone();
        }
        if let Some(ref result) = payload.result {
            detail["result"] = result.clone();
        }
        // A failed action's trace id is at least as valuable as a successful
        // one's: it is how the caller asks why it failed.
        if let Some(ref trace_id) = payload.trace_id {
            detail["trace_id"] = json!(trace_id);
        }
        if let Some(ref trace) = payload.trace {
            detail["trace"] = trace.clone();
        }
        if let Some(ref rid) = payload.request_id {
            detail["request_id"] = json!(rid);
        }
        if let Some(ref act) = payload.action {
            detail["action"] = json!(act);
        }
        if let Some(dur) = payload.duration_ms {
            detail["duration_ms"] = json!(dur);
        }
        if let Some(ref b) = payload.bridge {
            if let Ok(v) = serde_json::to_value(b) {
                detail["bridge"] = v;
            }
        }
        if let Some(ref s) = payload.nvda_status_snapshot {
            if let Ok(v) = serde_json::to_value(s) {
                detail["nvda_status_snapshot"] = v;
            }
        }

        vec![
            ContentBlock::text(summary),
            ContentBlock::text(serde_json::to_string_pretty(&detail).unwrap_or_default()),
        ]
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
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<ListToolsResult, ErrorData>> + Send + '_ {
        let tools = self.catalog_read().tools_for_mcp.clone();
        future::ready(Ok(ListToolsResult::with_all_items(tools)))
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

            if let Err(err) = Self::validate_input(&runtime, &params) {
                return Ok(CallToolResponse::Complete(CallToolResult::error(vec![
                    ContentBlock::text(err.to_string()),
                ])));
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
                        }),
                        result: None,
                        trace_id: None,
                        trace: None,
                    };
                    return Ok(CallToolResponse::Complete(CallToolResult::error(
                        Self::error_content(&error_envelope),
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
                Ok(CallToolResponse::Complete(CallToolResult::success(
                    Self::success_content(&bridge_result),
                )))
            } else {
                Ok(CallToolResponse::Complete(CallToolResult::error(
                    Self::error_content(&bridge_result),
                )))
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
            let events_path = format!("/events?since={cursor}&timeout_ms={timeout_ms}");
            match bridge_client.get_json(&events_path).await {
                Ok(page) => {
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
                    let changed = {
                        let current = tool_catalog
                            .read()
                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                        schema_hash_changed(
                            current.schema_hash.as_deref(),
                            new_catalog.schema_hash.as_deref(),
                        )
                    };
                    if changed {
                        let previous_hash = {
                            let mut current = tool_catalog
                                .write()
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                            let previous = current.schema_hash.clone();
                            *current = new_catalog;
                            previous
                        };
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
                    bail!("Failed to discover tools from bridge /tools after retries: {err}");
                }
            }
        }
    }
    let tool_catalog = catalog_opt.unwrap();

    info!(
        tool_count = tool_catalog.tools_for_mcp.len(),
        bridge_version = %tool_catalog.bridge_version,
        schema_hash = ?tool_catalog.schema_hash,
        "Discovered tools from bridge"
    );

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
        let catalog = BridgeClient::build_catalog(payload).expect("build_catalog failed");

        assert!(catalog.tools_for_mcp.len() > 0, "No tools found in schema.json");
        assert_eq!(catalog.tools_for_mcp.len(), catalog.runtime_by_name.len(), "Mismatch between tools_for_mcp and runtime_by_name sizes");
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

    #[test]
    fn server_capabilities_advertise_refreshable_tool_catalog() {
        let dummy_catalog = ToolCatalog {
            tools_for_mcp: Vec::new(),
            runtime_by_name: HashMap::new(),
            bridge_version: "1.0.0".into(),
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
            supported_api_levels: SUPPORTED_API_LEVELS.to_vec().into(),
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
            PROTOCOL_NAME, "1.0.0", "1.0.0", 2, &[1, 2], 1, 1,
        );
        let message = refused.expect_err("a bridge serving only level 2 cannot satisfy level 3");
        assert!(message.contains("does not support"), "{message}");
    }

    #[test]
    fn success_content_includes_result_block() {
        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"sent": true})),
            result: Some(json!({"ok": true, "state_version": 7, "changed": true})),
            ..Default::default()
        };

        let blocks = NvdaServer::success_content(&payload);
        let text = match &blocks[0] {
            ContentBlock::Text(text) => text.text.clone(),
            _ => panic!("expected text content"),
        };
        let envelope: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(envelope["result"]["state_version"], 7);
        assert_eq!(envelope["data"]["sent"], true);
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
    fn success_content_carries_the_trace_block() {
        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"sent": true})),
            trace_id: Some("at-abc".into()),
            trace: Some(json!({"trace_id": "at-abc", "outcome": "succeeded"})),
            ..Default::default()
        };

        let text = first_text(&NvdaServer::success_content(&payload));
        let envelope: Value = serde_json::from_str(&text).unwrap();

        assert_eq!(envelope["trace_id"], "at-abc");
        assert_eq!(envelope["trace"]["outcome"], "succeeded");
    }

    #[test]
    fn error_content_carries_the_trace_block() {
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

        let blocks = NvdaServer::error_content(&payload);
        // [summary, detail]
        let detail: Value = serde_json::from_str(&first_text(&blocks[1..])).unwrap();

        assert_eq!(detail["trace_id"], "at-def");
        assert_eq!(detail["trace"]["outcome"], "failed");
    }
}
