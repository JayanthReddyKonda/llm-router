#!/usr/bin/env python3
"""
TUI-Only Provider & Model Manager for LiteLLM Gateway
Clean, interactive terminal interface for managing:
  - Providers (Base URLs)
  - Multi-Key pools (per-key RPM rate limits)
  - Models (Aliases & upstream identifiers)

Zero external dependencies (pure Python standard library).
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List, Tuple

# ANSI Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))

# Primary state file in manager/, with fallback to config/ or root
STATE_FILE = os.path.join(MANAGER_DIR, "providers.json")
if not os.path.isfile(STATE_FILE):
    alt_config = os.path.join(PROJECT_ROOT, "config", "providers.json")
    if os.path.isfile(alt_config):
        STATE_FILE = alt_config

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")


def clear_screen():
    """Clear terminal screen for clean TUI presentation."""
    os.system("clear" if os.name != "nt" else "cls")


def load_env() -> Dict[str, str]:
    """Parse key=value pairs from .env."""
    env = {}
    if not os.path.isfile(ENV_FILE):
        return env
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_gateway_credentials() -> Tuple[str, str]:
    """Retrieve gateway base_url and master_key."""
    env = load_env()
    base_url = os.environ.get("LITELLM_BASE_URL") or env.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
    master_key = os.environ.get("LITELLM_MASTER_KEY") or env.get("LITELLM_MASTER_KEY", "")
    return base_url.rstrip("/"), master_key


def api_request(
    endpoint: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 15
) -> Dict[str, Any]:
    """Send authenticated HTTP request to LiteLLM Gateway."""
    base_url, master_key = get_gateway_credentials()
    if not master_key:
        return {"status": 0, "error": "LITELLM_MASTER_KEY not set"}

    url = f"{base_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json"
    }
    encoded = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=encoded, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return {"status": resp.status, "data": json.loads(body) if body else {}}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"error": body}
        return {"status": e.code, "error": parsed}
    except Exception as e:
        return {"status": 0, "error": str(e)}


# ==============================================================================
# State Persistence (manager/providers.json)
# ==============================================================================

def load_providers() -> Dict[str, Any]:
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_providers(data: Dict[str, Any]):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ==============================================================================
# Gateway Synchronization
# ==============================================================================

def sync_single_deployment(
    alias: str,
    upstream: str,
    key_str: str,
    base_url: Optional[str],
    rpm: Optional[int] = None,
    tpm: Optional[int] = None,
    provider: Optional[str] = None,
    provider_type: Optional[str] = None,
    key_id: Optional[str] = None
) -> bool:
    litellm_params: Dict[str, Any] = {
        "model": upstream,
        "api_key": key_str
    }
    if base_url and "generativelanguage.googleapis.com" not in base_url:
        litellm_params["api_base"] = base_url
    if provider_type:
        litellm_params["custom_llm_provider"] = provider_type
    if rpm:
        litellm_params["rpm"] = rpm
    if tpm:
        litellm_params["tpm"] = tpm

    payload = {
        "model_name": alias,
        "litellm_params": litellm_params,
        "model_info": {
            "provider": provider or "custom",
            "provider_type": provider_type or "",
            "key_id": key_id or "key",
            "base_url": base_url or ""
        }
    }
    res = api_request("/model/new", method="POST", data=payload)
    return res.get("status") in (200, 201)


def get_keys_list(provider_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Retrieve keys as a list of dicts {key, tpm, rpm}."""
    raw_keys = provider_data.get("keys", [])
    result = []
    for k in raw_keys:
        if isinstance(k, dict):
            result.append({
                "key": k.get("key", ""),
                "tpm": k.get("tpm"),
                "rpm": k.get("rpm")
            })
        else:
            result.append({
                "key": str(k),
                "tpm": None,
                "rpm": None
            })
    return result


def get_models_dict(provider_data: Dict[str, Any]) -> Dict[str, str]:
    """Retrieve models as a dict {alias: upstream}."""
    raw_models = provider_data.get("models", {})
    if isinstance(raw_models, dict):
        return raw_models
    elif isinstance(raw_models, list):
        res = {}
        for m in raw_models:
            if isinstance(m, dict):
                res[m.get("alias", "")] = m.get("upstream", "")
        return res
    return {}


def delete_all_deployments() -> int:
    """Fetch all current deployments from LiteLLM and delete them from PostgreSQL."""
    res = api_request("/model/info", method="GET")
    data = res.get("data", {}).get("data", [])
    if isinstance(data, dict):
        deployments = [data]
    else:
        deployments = data or []

    deleted = 0
    for d in deployments:
        m_id = d.get("model_info", {}).get("id") or d.get("model_id") or d.get("id")
        if m_id:
            del_res = api_request("/model/delete", method="POST", data={"id": m_id})
            if del_res.get("status") in (200, 201):
                deleted += 1
    return deleted


def sync_all(quiet: bool = False) -> Tuple[int, int]:
    providers = load_providers()

    # Reconcile: Purge existing deployments so PostgreSQL strictly mirrors providers.json
    delete_all_deployments()

    success = 0
    failed = 0

    for p_name, p_data in providers.items():
        base_url = p_data.get("base_url")
        provider_type = p_data.get("provider_type")
        keys = get_keys_list(p_data)
        models = get_models_dict(p_data)
        if not keys or not models:
            continue

        for alias, upstream in models.items():
            for idx, k in enumerate(keys, 1):
                if sync_single_deployment(
                    alias,
                    upstream,
                    k["key"],
                    base_url,
                    rpm=k.get("rpm"),
                    tpm=k.get("tpm"),
                    provider=p_name,
                    provider_type=provider_type,
                    key_id=f"key-{idx}"
                ):
                    success += 1
                else:
                    failed += 1

    if not quiet:
        print(f"\n{GREEN}✓ Stored and synced {success} model deployments directly into PostgreSQL!{RESET}")
        if failed:
            print(f"{YELLOW}({failed} deployments could not be updated or gateway is offline){RESET}")
        input(f"\nPress {BOLD}Enter{RESET} to continue...")
    return success, failed


def inspect_postgres_records():
    """Query and display live model records from PostgreSQL (LiteLLM_ProxyModelTable)."""
    clear_screen()
    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}{CYAN}      LIVE POSTGRESQL DATABASE RECORDS (LiteLLM_ProxyModelTable)      {RESET}")
    print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

    t0 = time.perf_counter()
    health = api_request("/health/liveliness", method="GET", timeout=4)
    latency_ms = (time.perf_counter() - t0) * 1000

    if health.get("status") != 200:
        print(f"  Gateway Status: {RED}{BOLD}OFFLINE{RESET}\n")
        print(f"{YELLOW}Could not connect to LiteLLM at {get_gateway_credentials()[0]}. Is the container running?{RESET}")
        print(f"Start it with: {BOLD}docker compose up -d{RESET}\n")
        input(f"Press {BOLD}Enter{RESET} to return to menu...")
        return

    print(f"  Gateway Status: {GREEN}{BOLD}ONLINE{RESET} ({latency_ms:.1f}ms latency)")
    res = api_request("/model/info", method="GET")
    if res.get("status") != 200:
        print(f"\n{YELLOW}Could not fetch database records. Error: {res.get('error')}{RESET}\n")
        input(f"Press {BOLD}Enter{RESET} to return to menu...")
        return

    data = res.get("data", {}).get("data", [])
    if isinstance(data, dict):
        deployments = [data]
    else:
        deployments = data or []

    if not deployments:
        print(f"{YELLOW}PostgreSQL table 'LiteLLM_ProxyModelTable' currently has 0 rows.{RESET}")
        print(f"Select option [4] 'Sync Everything to PostgreSQL Database' to populate it.\n")
        input(f"Press {BOLD}Enter{RESET} to return to menu...")
        return

    print(f"Total Rows in PostgreSQL: {BOLD}{len(deployments)}{RESET}\n")
    print(f"  {BOLD}{'MODEL ALIAS':<22} | {'UPSTREAM MODEL':<32} | {'RPM':<5} | {'TPM':<10} | {'BASE URL':<26} | {'MODEL ID'}{RESET}")
    print("  " + "-" * 115)

    for d in sorted(deployments, key=lambda x: (x.get("model_name", ""), str(x.get("litellm_params", {}).get("model", "")))):
        alias = d.get("model_name", "-")
        params = d.get("litellm_params", {})
        upstream = params.get("model", "-")
        rpm = str(params.get("rpm") or "-")
        tpm = str(params.get("tpm") or "-")
        base = params.get("api_base") or "-"
        m_id = d.get("model_info", {}).get("id") or d.get("model_id") or d.get("id") or "-"

        base_disp = (base[:23] + "...") if len(base) > 26 else base
        upstream_disp = (upstream[:29] + "...") if len(upstream) > 32 else upstream
        m_id_disp = (m_id[:10] + "...") if len(m_id) > 13 else m_id
        print(f"  {BOLD}{alias:<22}{RESET} | {upstream_disp:<32} | {rpm:<5} | {tpm:<10} | {base_disp:<26} | {DIM}{m_id_disp}{RESET}")

    print("  " + "-" * 115)
    print(f"\n{GREEN}✓ All records above are actively persisted in PostgreSQL container (omo-litellm-postgres).{RESET}\n")
    input(f"Press {BOLD}Enter{RESET} to return to menu...")


# ==============================================================================
# Helper Prompts & Displays
# ==============================================================================

def prompt(text: str, default: str = "") -> str:
    if default:
        res = input(f"{text} [{default}]: ").strip()
        return res if res else default
    return input(f"{text}: ").strip()


def mask_key(k: str) -> str:
    if len(k) > 16:
        return k[:7] + "..." + k[-4:]
    elif len(k) > 8:
        return k[:4] + "..."
    return "***"


def display_providers_table():
    providers = load_providers()
    if not providers:
        print(f"  {YELLOW}(No providers configured yet){RESET}\n")
        return []

    p_list = sorted(list(providers.keys()))
    print(f"  {BOLD}{'#':<3} | {'PROVIDER':<14} | {'TYPE':<12} | {'KEYS':<5} | {'MODELS':<7} | {'BASE URL'}{RESET}")
    print("  " + "-" * 80)
    for idx, name in enumerate(p_list, 1):
        p = providers[name]
        p_type = p.get("provider_type", "openai")
        k_count = len(get_keys_list(p))
        m_count = len(get_models_dict(p))
        base = p.get("base_url", "-")
        base_disp = (base[:28] + "...") if len(base) > 31 else base
        print(f"  {CYAN}{idx:<3}{RESET} | {BOLD}{name:<14}{RESET} | {p_type:<12} | {k_count:<5} | {m_count:<7} | {base_disp}")
    print("  " + "-" * 80)
    return p_list


def display_keys_table(provider: str):
    providers = load_providers()
    keys = get_keys_list(providers.get(provider, {}))
    if not keys:
        print(f"  {YELLOW}(No keys configured for {provider}){RESET}\n")
        return []

    print(f"\n  {BOLD}{'#':<3} | {'RPM':<6} | {'TPM LIMIT':<12} | {'API KEY (MASKED)'}{RESET}")
    print("  " + "-" * 60)
    for idx, k in enumerate(keys, 1):
        rpm_str = str(k.get("rpm") or "-")
        tpm_str = str(k.get("tpm") or "-")
        print(f"  {CYAN}{idx:<3}{RESET} | {rpm_str:<6} | {tpm_str:<12} | {DIM}{mask_key(k['key'])}{RESET}")
    print("  " + "-" * 60)
    return keys


def display_models_table(provider: str):
    providers = load_providers()
    models = get_models_dict(providers.get(provider, {}))
    if not models:
        print(f"  {YELLOW}(No models configured for {provider}){RESET}\n")
        return []

    print(f"\n  {BOLD}{'#':<3} | {'MODEL ALIAS':<22} | {'UPSTREAM IDENTIFIER'}{RESET}")
    print("  " + "-" * 60)
    m_list = list(models.items())
    for idx, (alias, upstream) in enumerate(m_list, 1):
        print(f"  {CYAN}{idx:<3}{RESET} | {BOLD}{alias:<22}{RESET} | {upstream}")
    print("  " + "-" * 60)
    return m_list


# ==============================================================================
# Keys Submenu (Level 2)
# ==============================================================================

def menu_keys(provider: str):
    while True:
        clear_screen()
        providers = load_providers()
        p_data = providers.get(provider, {})
        print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
        print(f"{BOLD}Manage Keys for: {GREEN}{provider}{RESET} (Type: {p_data.get('provider_type', 'openai')} | Base URL: {p_data.get('base_url', '-')})")
        print(f"{BOLD}{CYAN}======================================================================{RESET}")

        keys = display_keys_table(provider)

        print(f"\n{BOLD}Options:{RESET}")
        print("  [1] Add Key (with RPM & TPM limits)")
        print("  [2] Update Key / Rate Limits")
        print("  [3] Delete Key")
        print("  [0] Back to Provider Menu")

        choice = prompt("\nSelect choice [0-3]").strip()

        if choice == "1":
            print(f"\n{BOLD}--- Add Key to {provider} ---{RESET}")
            key_str = prompt("Enter API Key")
            if not key_str:
                print(f"{RED}Key is required.{RESET}")
                time.sleep(1)
                continue

            rpm_str = prompt("Requests Per Minute (RPM) limit (Enter for unlimited)")
            rpm = int(rpm_str) if rpm_str.isdigit() else None
            tpm_str = prompt("Tokens Per Minute (TPM) limit (Enter for unlimited)")
            tpm = int(tpm_str) if tpm_str.isdigit() else None

            current_keys = p_data.get("keys", [])
            current_keys.append({
                "key": key_str,
                "rpm": rpm,
                "tpm": tpm
            })
            p_data["keys"] = current_keys
            save_providers(providers)

            # Auto-sync across models
            models = get_models_dict(p_data)
            p_type = p_data.get("provider_type")
            for alias, upstream in models.items():
                sync_single_deployment(alias, upstream, key_str, p_data.get("base_url"), rpm=rpm, tpm=tpm, provider=provider, provider_type=p_type)

            print(f"\n{GREEN}✓ Added key (RPM: {rpm or 'Unlimited'}, TPM: {tpm or 'Unlimited'}) and deployed across {len(models)} model(s)!{RESET}")
            time.sleep(1.5)

        elif choice == "2":
            if not keys:
                continue
            num_str = prompt("\nEnter Key # to update")
            if not num_str.isdigit() or not (1 <= int(num_str) <= len(keys)):
                print(f"{RED}Invalid key selection.{RESET}")
                time.sleep(1)
                continue

            idx = int(num_str) - 1
            new_k = prompt("New API Key string (Enter to keep current)")
            new_rpm = prompt("New RPM limit (Enter to keep current)")
            new_tpm = prompt("New TPM limit (Enter to keep current)")
            current_keys = p_data.get("keys", [])
            target = current_keys[idx]
            if isinstance(target, dict):
                if new_k:
                    target["key"] = new_k
                if new_rpm:
                    target["rpm"] = int(new_rpm) if new_rpm.isdigit() else None
                if new_tpm:
                    target["tpm"] = int(new_tpm) if new_tpm.isdigit() else None
            else:
                target_dict = {
                    "key": new_k or target,
                    "rpm": int(new_rpm) if new_rpm and new_rpm.isdigit() else None,
                    "tpm": int(new_tpm) if new_tpm and new_tpm.isdigit() else None
                }
                current_keys[idx] = target_dict

            p_data["keys"] = current_keys
            save_providers(providers)
            sync_all(quiet=True)
            print(f"\n{GREEN}✓ Key updated and synced!{RESET}")
            time.sleep(1)

        elif choice == "3":
            if not keys:
                continue
            num_str = prompt("\nEnter Key # to delete")
            if not num_str.isdigit() or not (1 <= int(num_str) <= len(keys)):
                print(f"{RED}Invalid key selection.{RESET}")
                time.sleep(1)
                continue

            idx = int(num_str) - 1
            current_keys = p_data.get("keys", [])
            del current_keys[idx]
            p_data["keys"] = current_keys
            save_providers(providers)
            print(f"\n{GREEN}✓ Key deleted.{RESET}")
            time.sleep(1)

        elif choice in ("0", "b", "back", "q"):
            break


# ==============================================================================
# Models Submenu (Level 2)
# ==============================================================================

def menu_models(provider: str):
    while True:
        clear_screen()
        providers = load_providers()
        p_data = providers.get(provider, {})
        print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
        print(f"{BOLD}Manage Models for Provider: {GREEN}{provider}{RESET} (Base URL: {p_data.get('base_url', '-')})")
        print(f"{BOLD}{CYAN}======================================================================{RESET}")

        m_list = display_models_table(provider)

        print(f"\n{BOLD}Options:{RESET}")
        print("  [1] Add Model (Alias + Upstream Model)")
        print("  [2] Update Upstream Model")
        print("  [3] Delete Model")
        print("  [0] Back to Provider Menu")

        choice = prompt("\nSelect choice [0-3]").strip()

        if choice == "1":
            print(f"\n{BOLD}--- Add Model to {provider} ---{RESET}")
            alias = prompt("Model Alias (what clients call, e.g. glm-5-2, deepseek-v4)")
            p_type = p_data.get("provider_type") or provider
            upstream = prompt(f"Upstream Model identifier (e.g. {p_type}/deepseek-ai/model or deepseek-ai/model)")
            if not alias or not upstream:
                print(f"{RED}Both alias and upstream model are required.{RESET}")
                time.sleep(1)
                continue

            # Auto-normalize: if user didn't type the provider prefix, add it automatically
            if not (upstream.startswith(f"{p_type}/") or upstream.startswith(f"{provider}/")):
                upstream = f"{p_type}/{upstream}"

            models_dict = get_models_dict(p_data)
            models_dict[alias] = upstream
            p_data["models"] = models_dict
            save_providers(providers)

            # Auto-sync across all keys of this provider
            keys = get_keys_list(p_data)
            p_type = p_data.get("provider_type")
            for idx, k in enumerate(keys, 1):
                sync_single_deployment(
                    alias,
                    upstream,
                    k["key"],
                    p_data.get("base_url"),
                    rpm=k.get("rpm"),
                    tpm=k.get("tpm"),
                    provider=provider,
                    provider_type=p_type,
                    key_id=f"key-{idx}"
                )

            print(f"\n{GREEN}✓ Model '{alias}' added and deployed across {len(keys)} key(s) in PostgreSQL!{RESET}")
            time.sleep(1.5)

        elif choice == "2":
            if not m_list:
                continue
            num_str = prompt("\nEnter Model # to update")
            if not num_str.isdigit() or not (1 <= int(num_str) <= len(m_list)):
                print(f"{RED}Invalid model selection.{RESET}")
                time.sleep(1)
                continue

            target_alias = m_list[int(num_str) - 1][0]
            new_up = prompt(f"New Upstream Model identifier for '{target_alias}'")
            if new_up:
                models_dict = get_models_dict(p_data)
                models_dict[target_alias] = new_up
                p_data["models"] = models_dict
                save_providers(providers)
                sync_all(quiet=True)
                print(f"\n{GREEN}✓ Model updated and synced!{RESET}")
                time.sleep(1)

        elif choice == "3":
            if not m_list:
                continue
            num_str = prompt("\nEnter Model # to delete")
            if not num_str.isdigit() or not (1 <= int(num_str) <= len(m_list)):
                print(f"{RED}Invalid model selection.{RESET}")
                time.sleep(1)
                continue

            target_alias = m_list[int(num_str) - 1][0]
            models_dict = get_models_dict(p_data)
            if target_alias in models_dict:
                del models_dict[target_alias]
                p_data["models"] = models_dict
                save_providers(providers)
                print(f"\n{GREEN}✓ Model '{target_alias}' deleted.{RESET}")
                time.sleep(1)

        elif choice in ("0", "b", "back", "q"):
            break


# ==============================================================================
# Provider Submenu (Level 1 Drilldown)
# ==============================================================================

def menu_provider_drilldown(provider: str):
    while True:
        clear_screen()
        providers = load_providers()
        if provider not in providers:
            break

        p_data = providers[provider]
        k_count = len(p_data.get("keys", []))
        m_count = len(p_data.get("models", []))

        print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
        print(f"{BOLD}Provider: {GREEN}{provider}{RESET} (Type: {p_data.get('provider_type', 'openai')})")
        print(f"Base URL: {p_data.get('base_url', '-')}")
        print(f"Status:   {k_count} Key(s) | {m_count} Model(s)")
        print(f"{BOLD}{CYAN}======================================================================{RESET}")
        print("  [1] Manage Keys (Add / Update / Delete Keys, RPM & TPM Limits)")
        print("  [2] Manage Models (Add / Update / Delete Models)")
        print("  [3] Update Base URL")
        print("  [4] Update Provider Type")
        print("  [0] Back to All Providers")

        choice = prompt("\nSelect choice [0-4]").strip()

        if choice == "1":
            menu_keys(provider)
        elif choice == "2":
            menu_models(provider)
        elif choice == "3":
            new_base = prompt(f"New Base URL for {provider}", default=p_data.get("base_url", ""))
            if new_base:
                p_data["base_url"] = new_base
                save_providers(providers)
                sync_all(quiet=True)
                print(f"\n{GREEN}✓ Base URL updated!{RESET}")
                time.sleep(1)
        elif choice == "4":
            new_type = prompt(f"New Provider Type for {provider}", default=p_data.get("provider_type", "openai"))
            if new_type:
                p_data["provider_type"] = new_type
                save_providers(providers)
                sync_all(quiet=True)
                print(f"\n{GREEN}✓ Provider type updated!{RESET}")
                time.sleep(1)
        elif choice in ("0", "b", "back", "q"):
            break


# ==============================================================================
# Main Menu (Level 1)
# ==============================================================================

def main_menu():
    while True:
        clear_screen()
        print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
        print(f"{BOLD}{CYAN}              LLM ROUTER: DYNAMIC PROVIDER MANAGER                   {RESET}")
        print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

        p_list = display_providers_table()

        print(f"\n{BOLD}Actions:{RESET}")
        print("  [1] Add New Provider (Name + Base URL)")
        print("  [2] Select Provider to Manage (Keys & Models)")
        print("  [3] Delete a Provider")
        print("  [4] Sync Everything to PostgreSQL Database")
        print("  [5] Inspect PostgreSQL Live Records (LiteLLM_ProxyModelTable)")
        print("  [0] Exit")

        choice = prompt(f"\n{BOLD}Select an option [0-5]{RESET}").strip()

        if choice == "1":
            print(f"\n{BOLD}--- Add New Provider ---{RESET}")
            name = prompt("Provider Name (e.g. nvidia, groq, deepseek, vllm)")
            p_type = prompt("Provider Type (e.g. openai, gemini, nvidia_nim, openrouter)", default="openai")
            base = prompt("Base URL (e.g. https://api.groq.com/openai/v1)")
            if name and base:
                providers = load_providers()
                p_key = name.strip().lower()
                providers[p_key] = {
                    "provider_type": p_type.strip(),
                    "base_url": base.strip(),
                    "keys": [],
                    "models": {}
                }
                save_providers(providers)
                print(f"\n{GREEN}✓ Provider '{p_key}' created!{RESET}")
                time.sleep(1)
                # Jump straight into managing it
                menu_provider_drilldown(p_key)
            else:
                print(f"{RED}Both name and base URL are required.{RESET}")
                time.sleep(1)

        elif choice == "2":
            if not p_list:
                print(f"{YELLOW}Add a provider first.{RESET}")
                time.sleep(1)
                continue
            sel = prompt("\nEnter Provider # or Name to manage").strip().lower()
            target = None
            if sel.isdigit() and 1 <= int(sel) <= len(p_list):
                target = p_list[int(sel) - 1]
            elif sel in p_list:
                target = sel
            if target:
                menu_provider_drilldown(target)
            else:
                print(f"{RED}Invalid provider selection.{RESET}")
                time.sleep(1)

        elif choice == "3":
            if not p_list:
                continue
            sel = prompt("\nEnter Provider # or Name to delete").strip().lower()
            target = None
            if sel.isdigit() and 1 <= int(sel) <= len(p_list):
                target = p_list[int(sel) - 1]
            elif sel in p_list:
                target = sel
            if target:
                confirm = prompt(f"Are you sure you want to delete '{target}' and all its keys/models? (y/N)").lower()
                if confirm in ("y", "yes"):
                    providers = load_providers()
                    del providers[target]
                    save_providers(providers)
                    print(f"\n{GREEN}✓ Provider '{target}' deleted.{RESET}")
                    time.sleep(1)

        elif choice == "4":
            sync_all()

        elif choice == "5":
            inspect_postgres_records()

        elif choice in ("0", "q", "exit"):
            clear_screen()
            print(f"\n{BOLD}{GREEN}Done! Your dynamic configuration is saved.{RESET}\n")
            break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("sync", "--sync", "-s"):
        sync_all(quiet=False)
    elif len(sys.argv) > 1 and sys.argv[1].lower() in ("inspect", "--inspect", "-i"):
        inspect_postgres_records()
    else:
        main_menu()
