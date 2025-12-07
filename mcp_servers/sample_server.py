from fastmcp import FastMCP

mcp = FastMCP("Sample Server")

@mcp.tool()
def add(x: int, y: int) -> int:
    """Adds two numbers"""
    return x + y

@mcp.tool()
def echo(message: str) -> str:
    """Echoes a message"""
    return f"Echo: {message}"

if __name__ == "__main__":
    mcp.run()
