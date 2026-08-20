from pathlib import Path

from full_view_agent.contract_export import export_contracts

if __name__ == "__main__":
    # Contracts are versioned inside the agent-runtime repo (parents[1] =
    # repo root) so they travel with the code that generates them.
    contract_root = Path(__file__).resolve().parents[1] / "contracts"
    export_contracts(contract_root)
    print(f"Contracts exported to {contract_root}")
