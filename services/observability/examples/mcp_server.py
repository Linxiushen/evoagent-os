"""Tiny MCP server for exercising HarnessLab's optional stdio bridge."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("harnesslab-example")


@mcp.tool()
def deployment_risk(service: str, changed_files: int) -> dict[str, object]:
    """Estimate deployment risk from a deterministic fixture."""
    level = "high" if changed_files > 20 else "moderate" if changed_files > 5 else "low"
    return {"service": service, "changed_files": changed_files, "risk": level}


if __name__ == "__main__":
    mcp.run(transport="stdio")

