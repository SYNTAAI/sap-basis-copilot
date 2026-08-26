"""Settings for SAP Basis Copilot.

Environment names match the main SyntaAI Agent deployment so one .env can drive
both. Where that deployment uses a different spelling, both are accepted:
SAP_ASHOST / SAP_HOST, SAP_PASSWD / SAP_PASSWORD, JCO_BASE_URL / JCO_SERVICE_URL.
"""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    # --- SAP connection ---
    sap_ashost: str = Field(default="", validation_alias=AliasChoices("SAP_ASHOST", "SAP_HOST"))
    sap_sysnr: str = Field(default="00", validation_alias=AliasChoices("SAP_SYSNR"))
    sap_client: str = Field(default="100", validation_alias=AliasChoices("SAP_CLIENT"))
    sap_user: str = Field(default="", validation_alias=AliasChoices("SAP_USER"))
    sap_passwd: str = Field(default="", validation_alias=AliasChoices("SAP_PASSWD", "SAP_PASSWORD"))
    sap_lang: str = Field(default="EN", validation_alias=AliasChoices("SAP_LANG"))

    # --- local JCo REST bridge ---
    jco_base_url: str = Field(default="http://127.0.0.1:8080",
                              validation_alias=AliasChoices("JCO_BASE_URL", "JCO_SERVICE_URL"))
    rfc_timeout_seconds: float = Field(default=60.0, validation_alias=AliasChoices("RFC_TIMEOUT_SECONDS"))

    # --- transport ---
    # stdio is the default: the normal deployment is Claude Desktop on an
    # administrator's own machine. streamable-http is for a hosted install.
    mcp_transport: str = Field(default="stdio", validation_alias=AliasChoices("MCP_TRANSPORT"))
    mcp_host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("MCP_HOST"))
    mcp_port: int = Field(default=8090, validation_alias=AliasChoices("MCP_PORT"))

    # --- auth (hosted mode only; ignored for stdio) ---
    mcp_no_auth: str = Field(default="", validation_alias=AliasChoices("MCP_NO_AUTH"))
    issuer_url: str = Field(default="http://127.0.0.1:8090",
                            validation_alias=AliasChoices("SYNTAAI_ISSUER_URL", "ISSUER_URL"))
    data_dir: str = Field(default="./data", validation_alias=AliasChoices("SYNTAAI_DATA_DIR", "DATA_DIR"))
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("LOG_LEVEL"))

    @property
    def transport(self) -> str:
        return (self.mcp_transport or "stdio").strip().lower()

    @property
    def auth_disabled(self) -> bool:
        """stdio is a local pipe with no network surface, so OAuth is off by default."""
        if self.transport == "stdio":
            return True
        return str(self.mcp_no_auth).strip().lower() in ("1", "true", "yes")
