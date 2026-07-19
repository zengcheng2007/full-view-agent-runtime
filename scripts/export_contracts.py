from pathlib import Path

from full_view_agent.contract_export import export_contracts

if __name__ == "__main__":
    contract_root = Path(__file__).resolve().parents[2] / "contracts"
    export_contracts(contract_root)
    print(f"Contracts exported to {contract_root}")
