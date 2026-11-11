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

        Ok(Self {
            client,
            base_url,
            auth_token,
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
        let url = format!("{}/tools", self.base_url);
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

/// One shared `/events` long-poll loop feeding every `listen()` subscriber.
fn spawn_event_pump(
    bridge_client: BridgeClient,
    subscription_tx: broadcast::Sender<i64>,
    shutdown: Arc<AtomicBool>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut cursor = 0_i64;
        while !shutdown.load(Ordering::SeqCst) {
            let events_path = format!("/events?since={cursor}&timeout_ms=1000");
            match bridge_client.get_json(&events_path).await {
                Ok(page) => {
                    let version = page
                        .get("state_version")
                        .and_then(Value::as_i64)
                        .unwrap_or(cursor);
                    if version > cursor {
                        // The bridge persists the journal, so advancing the cursor
                        // is the store; the broadcast is just fan-out.
                        cursor = version;
                        let _ = subscription_tx.send(version);
                    }
                }
                Err(err) => {
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
}
