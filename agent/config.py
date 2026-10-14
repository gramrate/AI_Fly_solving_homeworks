import os
from pathlib import Path

import yaml

ENV = {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY", "model": "OPENAI_MODEL"}


def load_config(path: str) -> dict:
    """Load config.yaml; relative paths resolve against the config file, env vars override the llm block."""
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    llm = cfg.setdefault("llm", {})
    for key, env in ENV.items():
        if os.environ.get(env):
            llm[key] = os.environ[env]
    cfg["policy"] = str(cfg_path.parent / cfg.get("policy", "policy.yaml"))
    cfg["runs_dir"] = str(cfg_path.parent / cfg.get("runs_dir", "runs"))
    cfg.setdefault("mode", "safe")
    cfg.setdefault("limits", {}).setdefault("bash_timeout", 30)
    cfg["limits"].setdefault("max_output", 8000)
    return cfg
