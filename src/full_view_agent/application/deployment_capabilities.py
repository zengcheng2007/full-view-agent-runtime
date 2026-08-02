HOUSING_NEXT_AREA_ENV = "FULL_VIEW_HOUSING_NEXT_AREA_ENABLED"


def parse_housing_next_area_enabled(raw_value: str | None) -> bool:
    """Only an explicit ``true`` enables the deployment capability."""
    if raw_value is None or not raw_value.strip():
        return False
    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise RuntimeError(f"{HOUSING_NEXT_AREA_ENV} must be 'true' or 'false'")
