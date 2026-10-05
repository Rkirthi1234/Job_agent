"""Application configuration loaded from environment variables."""
import os
from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "AI Job Agent"
    environment: str = "development"
    
    @property
    def database_url(self) -> str:
        """Database URL pointing to data/ai_job_agent.db"""
        data_path = os.path.join(os.path.dirname(__file__), '../data')
        os.makedirs(data_path, exist_ok=True)
        db_path = os.path.join(data_path, 'ai_job_agent.db')
        return f"sqlite:///{db_path}"

    upload_dir: str = "data/uploads"
    max_file_size_mb: int = 10
    llm_provider: str = "ollama"
    llm_model: str = "llama3.1"
    llm_base_url: str = "http://localhost:11434/v1"
    openai_api_key: str = ""
    llm_timeout_seconds: int = 60
    llm_json_response_format: bool = True
    jooble_api_key: str = ""
    jooble_base_url: str = "https://jooble.org/api"
    real_application_enabled: bool = True
    real_application_timeout_seconds: float = 15.0
    real_application_supported_destinations: str = ""
    playwright_headless: bool = True
    application_artifacts_dir: str = "data/artifacts"
    captcha_resume_timeout_seconds: float = 1800.0
    test_application_current_company: str = ""
    test_application_linkedin_url: str = ""
    test_application_skip_submit: bool = False
    wellfound_user_data_dir: str = ""
    wellfound_cookies_path: str = ""
    wellfound_use_browser_use: bool = False
    # Monster JOB DISCOVERY (app/integrations/job_sources/monster.py), driven
    # by Browser Use. MONSTER_USER_DATA_DIR is the persistent Chrome profile
    # reused across runs (relative paths resolve against the project root).
    # MONSTER_HEADLESS defaults to false so a real, visible browser is used.
    monster_user_data_dir: str = "monster_profile"
    monster_headless: bool = False
    # Monster APPLICATION adapter (application_sources/monster.py). It reuses
    # the two settings above (same persistent, hand-signed-in Chrome profile;
    # this app never types Monster credentials). MONSTER_AUTO_SUBMIT defaults
    # to false: unless it is explicitly true the adapter prepares the form
    # and stops for a human, and SAFE TEST MODE (test_application_skip_submit)
    # always wins over it.
    monster_auto_submit: bool = False
    # How long (seconds) a Monster -> external (hitayu.live) application waits,
    # in the SAME browser run, for the user to finish signing in BY HAND when
    # the external site asks for authentication (Hitayu login / Microsoft
    # sign-in). This app never types credentials or presses sign-in controls;
    # it only watches the page. 0 disables the wait: the run then stops at the
    # sign-in page (blocker external_authentication_required). Configurable via
    # MONSTER_EXTERNAL_AUTH_TIMEOUT_SECONDS.
    monster_external_auth_timeout_seconds: float = 600.0
    # EXPLICIT OPT-IN Monster automatic login. Off by default: unless
    # MONSTER_AUTO_LOGIN is set to true, the adapter never types a
    # username/password and relies on the persistent Chrome profile being
    # already signed in. MONSTER_EMAIL / MONSTER_PASSWORD are the candidate's
    # own Monster account credentials, read only from the environment/.env.
    # The password is a SecretStr so repr()/str()/logging can never reveal it.
    monster_auto_login: bool = False
    monster_email: str = ""
    monster_password: SecretStr = SecretStr("")

    # EXPLICIT OPT-IN Wellfound automatic login. Off by default: unless
    # WELLFOUND_AUTO_LOGIN is set to true, the adapter never types a
    # username/password anywhere and the manual-login flow (see
    # wellfound_login_wait_seconds below) is unchanged. WELLFOUND_EMAIL /
    # WELLFOUND_PASSWORD are the candidate's OWN Wellfound account
    # details, read only from the environment/.env (never from the
    # database or an API request). The password is a SecretStr so that
    # repr()/str()/logging/serialisation of Settings can never reveal it;
    # it is unwrapped in exactly one place -- wellfound_auth.py, at the
    # moment it is typed into Wellfound's own login form. This app never
    # creates a Wellfound guest account or generates a password.
    wellfound_auto_login: bool = False
    wellfound_email: str = ""
    wellfound_password: SecretStr = SecretStr("")
    wellfound_auto_submit: bool = False
    wellfound_manual_wait_seconds: int = 60
    # How long (seconds) to keep a non-headless browser open, after
    # clicking Wellfound's "Log in with your account" link, waiting for
    # the CANDIDATE to manually finish signing in themselves. Only used
    # when WELLFOUND_AUTO_LOGIN is false -- see
    # WellfoundApplicationSource.ensure_authenticated() in wellfound.py.
    # Configurable via WELLFOUND_LOGIN_WAIT_SECONDS. 0 disables the wait
    # entirely (headless runs always skip it, since there is no human
    # present to complete login). Only needed on the FIRST run: once the
    # persistent profile (WELLFOUND_USER_DATA_DIR) holds a signed-in
    # session, later runs are already authenticated and never wait.
    wellfound_login_wait_seconds: int = 120
    # How long (seconds) to wait for application data/list elements to
    # appear on the Wellfound Applications/Applied area after a reload.
    # The page may initially show no applications (null/empty) immediately
    # after navigation; this configurable wait allows the dynamic rendering
    # to complete before concluding that the application wasn't submitted.
    # Configurable via WELLFOUND_APPLICATIONS_LOAD_WAIT_SECONDS.
    # Default: 5 seconds. Increase if your network is slow or Wellfound's
    # rendering is taking longer than expected.
    wellfound_applications_load_wait_seconds: int = 5
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def real_application_destination_map(self) -> dict[str, str]:
        destinations: dict[str, str] = {}
        raw = self.real_application_supported_destinations.strip()
        if not raw:
            return destinations
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            domain, _, submit_url = entry.partition("=")
            domain = domain.strip()
            submit_url = submit_url.strip()
            if domain and submit_url:
                destinations[domain] = submit_url
        return destinations

@lru_cache
def get_settings() -> Settings:
    return Settings()
