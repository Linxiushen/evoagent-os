import json

from evoagent_contracts.schema import export_schema, schema_bundle


def test_schema_bundle_indexes_public_models() -> None:
    schema = schema_bundle()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["models"]["UnifiedEvent"] == {"$ref": "#/$defs/UnifiedEvent"}
    event_properties = schema["$defs"]["UnifiedEvent"]["properties"]
    expected = {
        "source",
        "type",
        "correlation",
        "causation",
        "tenant",
        "workspace",
        "run",
        "workflow",
        "node",
        "payload",
        "timestamp",
    }
    assert expected <= event_properties.keys()


def test_export_schema_writes_valid_json(tmp_path) -> None:
    destination = tmp_path / "contracts.json"
    export_schema(destination)

    exported = json.loads(destination.read_text(encoding="utf-8"))
    assert exported["x-contract-version"] == "0.1.0"
    assert "Overview" in exported["models"]
