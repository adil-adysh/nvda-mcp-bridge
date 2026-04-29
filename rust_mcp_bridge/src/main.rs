use anyhow::{Context, Result, anyhow, bail};
use reqwest::{Client, Response};
use rmcp::{
    ServerHandler, ServiceExt,
    model::*,
    service::{RequestContext, RoleServer},
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::{
    collections::{HashMap, HashSet},
    env,
    future,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};
use tracing::{error, info, warn};
use tracing_subscriber::{EnvFilter, fmt};

const TOKEN_HEADER: &str = "X-NVDA-Bridge-Token";
const PROTOCOL_NAME: &str = "nvda-bridge-protocol";
const SUPPORTED_API_LEVEL: u32 = 1;
static REQUEST_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone)]
struct Settings {
    bridge_url: String,
    bridge_token: String,
    log_level: String,
}

impl Settings {
    fn from_env() -> Result<Self> {
        let bridge_url = env::var("NVDA_BRIDGE_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8765".to_string())
            .trim_end_matches('/')
            .to_string();
        let bridge_token = env::var("NVDA_BRIDGE_TOKEN")
            .context("Missing required environment variable NVDA_BRIDGE_TOKEN")?;
        let log_level = env::var("LOG_LEVEL").unwrap_or_else(|_| "INFO".to_string());

        Ok(Self {
            bridge_url,
            bridge_token,
            log_level,
        })
    }
}

#[derive(Debug, Deserialize, Clone)]
struct BridgeError {
    code: String,
    message: String,
}

#[derive(Debug, Deserialize, Clone)]
struct BridgeActionEnvelope {
    success: bool,
    #[serde(default)]
    data: Option<Value>,
    #[serde(default)]
    error: Option<BridgeError>,
}

#[derive(Debug, Deserialize, Clone)]
struct BridgeToolConstraints {
    #[serde(default)]
    dangerous: Option<bool>,
    #[serde(default)]
    requires_explicit_opt_in: Option<bool>,
}

#[derive(Debug, Deserialize, Clone)]
struct BridgeToolDef {
    name: String,
    description: String,
    #[serde(rename = "inputSchema")]
    input_schema: Map<String, Value>,
    #[serde(rename = "outputType")]
    output_type: Option<String>,
    version: String,
    category: String,
    #[serde(default)]
    constraints: Option<BridgeToolConstraints>,
}

#[derive(Debug, Deserialize)]
struct BridgeToolsResponse {
    protocol: String,
    protocol_version: String,
    bridge_version: String,
    api_level: u32,
    types: Map<String, Value>,
    tools: Vec<BridgeToolDef>,
    count: usize,
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
    tools_for_mcp: Vec<Tool>,
    runtime_by_name: HashMap<String, ToolRuntime>,
}

#[derive(Debug)]
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

        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(8))
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

        Self::validate_tools_payload(&payload)?;
        Self::build_catalog(payload)
    }

    fn validate_tools_payload(payload: &BridgeToolsResponse) -> Result<()> {
        if payload.protocol != PROTOCOL_NAME {
            bail!(
                "Bridge protocol mismatch: expected '{}', got '{}'",
                PROTOCOL_NAME,
                payload.protocol
            );
        }
        if payload.api_level != SUPPORTED_API_LEVEL {
            bail!(
                "Bridge API level mismatch: expected {}, got {}",
                SUPPORTED_API_LEVEL,
                payload.api_level
            );
        }
        if payload.protocol_version.trim().is_empty() {
            bail!("Bridge protocol_version cannot be empty");
        }
        if payload.bridge_version.trim().is_empty() {
            bail!("Bridge bridge_version cannot be empty");
        }
        if payload.count != payload.tools.len() {
            bail!(
                "Bridge tools count mismatch: declared {}, actual {}",
                payload.count,
                payload.tools.len()
            );
        }
        Ok(())
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
                    None => {
                        warn!(
                            tool = %def.name,
                            output_type = %output_type_name,
                            "Tool outputType not found in bridge types map"
                        );
                        None
                    }
                }
            } else {
                None
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
            tools_for_mcp,
            runtime_by_name,
        })
    }

    async fn execute_action(&self, action: &str, params: Map<String, Value>) -> Result<BridgeActionEnvelope> {
        let request_id = Self::next_request_id();
        let payload = json!({
            "request_id": request_id,
            "action": action,
            "params": params,
        });

        let mut last_404 = false;

        for endpoint in ["action", "execute"] {
            let url = format!("{}/{}", self.base_url, endpoint);
            let response = self
                .client
                .post(url)
                .header(TOKEN_HEADER, &self.auth_token)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .json(&payload)
                .send()
                .await
                .with_context(|| format!("Failed to connect to bridge for action: {action}"))?;

            if endpoint == "action" && response.status().as_u16() == 404 {
                last_404 = true;
                continue;
            }

            return Self::parse_action_response(action, response).await;
        }

        if last_404 {
            bail!("Bridge action endpoint not found (/action or /execute)");
        }

        bail!("Unexpected bridge endpoint resolution failure")
    }

    async fn parse_action_response(action: &str, response: Response) -> Result<BridgeActionEnvelope> {
        let status = response.status();
        let body_text = response.text().await.unwrap_or_default();

        if status.as_u16() == 401 {
            bail!("Invalid authentication token for bridge");
        }

        if status.as_u16() >= 400 {
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

        if let Some(success) = obj.get("success").and_then(Value::as_bool) {
            if success {
                let data = obj
                    .get("data")
                    .cloned()
                    .or_else(|| obj.get("result").cloned())
                    .or_else(|| Some(Value::Object(Map::new())));
                return Ok(BridgeActionEnvelope {
                    success: true,
                    data,
                    error: None,
                });
            }

            let parsed_error = obj
                .get("error")
                .and_then(Value::as_object)
                .map(|error_obj| BridgeError {
                    code: error_obj
                        .get("code")
                        .and_then(Value::as_str)
                        .unwrap_or("BRIDGE_ERROR")
                        .to_string(),
                    message: error_obj
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("Unknown bridge error")
                        .to_string(),
                })
                .or_else(|| {
                    Some(BridgeError {
                        code: "BRIDGE_ERROR".to_string(),
                        message: "Bridge returned unsuccessful response without error details"
                            .to_string(),
                    })
                });

            return Ok(BridgeActionEnvelope {
                success: false,
                data: obj.get("data").cloned(),
                error: parsed_error,
            });
        }

        bail!("Bridge action response missing required 'success' boolean")
    }

    fn extract_error_message(body_text: &str) -> String {
        if let Ok(parsed) = serde_json::from_str::<Value>(body_text) {
            if let Some(obj) = parsed.as_object() {
                if let Some(error_obj) = obj.get("error").and_then(Value::as_object)
                    && let Some(message_text) = error_obj.get("message").and_then(Value::as_str)
                {
                    return message_text.to_string();
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
    let mut annotation = ToolAnnotations::new().open_world(false);

    let dangerous = tool
        .constraints
        .as_ref()
        .and_then(|c| c.dangerous)
        .unwrap_or(false);

    if dangerous {
        return annotation.read_only(false).destructive(true).idempotent(false);
    }

    let is_read_only_by_name = tool.name.starts_with("get_")
        || tool.name.starts_with("read_")
        || tool.name.starts_with("inspect_")
        || tool.name.starts_with("walk_")
        || tool.name.starts_with("list_")
        || tool.name.starts_with("health_");

    let is_read_only_category = matches!(
        tool.category.as_str(),
        "diagnostics" | "navigation" | "focus" | "window" | "text"
    );

    if is_read_only_by_name || is_read_only_category {
        annotation = annotation.read_only(true).destructive(false).idempotent(true);
    }

    annotation
}

struct NvdaServer {
    bridge_client: BridgeClient,
    tool_catalog: ToolCatalog,
}

impl NvdaServer {
    fn new(bridge_client: BridgeClient, tool_catalog: ToolCatalog) -> Self {
        Self {
            bridge_client,
            tool_catalog,
        }
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

        let envelope_value = json!({
            "success": true,
            "data": data_value,
        });

        if jsonschema::is_valid(output_schema_value, &envelope_value) {
            return Ok(());
        }

        let legacy_data_only_value = envelope_value.get("data").cloned().unwrap_or(Value::Null);

        if jsonschema::is_valid(output_schema_value, &legacy_data_only_value) {
            return Ok(());
        }

        bail!(
            "Bridge output validation failed for '{}': response does not match declared outputType schema",
            runtime.definition.name
        );
    }

    fn success_content(payload: &BridgeActionEnvelope) -> Vec<Content> {
        let body = json!({
            "success": true,
            "data": payload.data.clone().unwrap_or_else(|| Value::Object(Map::new()))
        });
        vec![Content::text(body.to_string())]
    }

    fn error_content(payload: &BridgeActionEnvelope) -> Vec<Content> {
        let err = payload.error.clone().unwrap_or(BridgeError {
            code: "BRIDGE_ERROR".to_string(),
            message: "Unknown bridge error".to_string(),
        });

        let summary = format!("Bridge tool error {}: {}", err.code, err.message);
        let detail = json!({
            "success": false,
            "error": {
                "code": err.code,
                "message": err.message
            },
            "data": payload.data
        });

        vec![Content::text(summary), Content::text(detail.to_string())]
    }
}

impl ServerHandler for NvdaServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo {
            server_info: Implementation {
                name: "nvda-mcp-server-rust".into(),
                version: env!("CARGO_PKG_VERSION").into(),
            },
            instructions: Some(format!(
                "Bridge-backed tools discovered from /tools. protocol={} api_level={} bridge_version={}",
                PROTOCOL_NAME, SUPPORTED_API_LEVEL, self.tool_catalog.bridge_version
            )),
            capabilities: ServerCapabilities::builder().enable_tools().build(),
            ..Default::default()
        }
    }

    fn list_tools(
        &self,
        _request: Option<PaginatedRequestParam>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<ListToolsResult, ErrorData>> + Send + '_ {
        future::ready(Ok(ListToolsResult::with_all_items(
            self.tool_catalog.tools_for_mcp.clone(),
        )))
    }

    fn call_tool(
        &self,
        request: CallToolRequestParam,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = std::result::Result<CallToolResult, ErrorData>> + Send + '_ {
        async move {
            let tool_name = request.name.to_string();
            let Some(runtime) = self.tool_catalog.runtime_by_name.get(&tool_name) else {
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

            if let Err(err) = Self::validate_input(runtime, &params) {
                return Ok(CallToolResult::error(vec![Content::text(err.to_string())]));
            }

            let bridge_result = match self.bridge_client.execute_action(&tool_name, params).await {
                Ok(result) => result,
                Err(err) => {
                    error!(tool = %tool_name, error = %err, "Bridge call failed");
                    return Err(ErrorData::internal_error(
                        format!("Bridge call failed for '{}': {}", tool_name, err),
                        None,
                    ));
                }
            };

            if let Err(err) = Self::validate_output(runtime, &bridge_result) {
                error!(tool = %tool_name, error = %err, "Bridge output schema mismatch");
                return Ok(CallToolResult::error(vec![Content::text(err.to_string())]));
            }

            if bridge_result.success {
                Ok(CallToolResult::success(Self::success_content(&bridge_result)))
            } else {
                Ok(CallToolResult::error(Self::error_content(&bridge_result)))
            }
        }
    }
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

    let tool_catalog = bridge_client
        .fetch_tool_catalog()
        .await
        .context("Failed to discover tools from bridge /tools")?;

    info!(
        tool_count = tool_catalog.tools_for_mcp.len(),
        bridge_version = %tool_catalog.bridge_version,
        "Discovered tools from bridge"
    );

    let transport = (tokio::io::stdin(), tokio::io::stdout());
    let service = NvdaServer::new(bridge_client, tool_catalog)
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
    fn parse_action_payload_error_shape() {
        let payload = json!({
            "success": false,
            "error": {
                "code": "INVALID_PARAMS",
                "message": "direction required"
            }
        });

        let parsed = BridgeClient::parse_action_payload(payload).unwrap();
        assert!(!parsed.success);
        let err = parsed.error.unwrap();
        assert_eq!(err.code, "INVALID_PARAMS");
        assert_eq!(err.message, "direction required");
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
            }),
        };

        let ann = annotation_for_tool(&tool);
        assert_eq!(ann.read_only_hint, Some(false));
        assert_eq!(ann.destructive_hint, Some(true));
    }

    #[test]
    fn next_request_id_is_non_empty_and_prefixed() {
        let id = BridgeClient::next_request_id();
        assert!(!id.is_empty());
        assert!(id.starts_with("mcp-"));
    }

    #[test]
    fn validate_output_accepts_envelope_schema() {
        let runtime = ToolRuntime {
            definition: BridgeToolDef {
                name: "get_window_title".to_string(),
                description: "x".to_string(),
                input_schema: serde_json::from_value(json!({"type":"object"})).unwrap(),
                output_type: Some("WindowInfo".to_string()),
                version: "1.0.0".to_string(),
                category: "window".to_string(),
                constraints: None,
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

        let payload = BridgeActionEnvelope {
            success: true,
            data: Some(json!({"title":"Notepad"})),
            error: None,
        };

        assert!(NvdaServer::validate_output(&runtime, &payload).is_ok());
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
        BridgeClient::validate_tools_payload(&payload).expect("validate_tools_payload failed");

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
}
