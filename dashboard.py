import asyncio
import glob
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional
from nicegui import ui, app

# Ensure mcp_servers directory exists
if not os.path.exists("mcp_servers"):
    os.makedirs("mcp_servers")

@dataclass
class ServerConfig:
    port: int = 8000
    app_name: str = "mcp"
    use_https: bool = False
    ssl_cert_path: str = ""
    ssl_key_path: str = ""

class Server:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.name = os.path.basename(filepath).replace(".py", "")
        self.config = ServerConfig()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: List[str] = []
        self.log_container = None
        self.status_label = None

    @property
    def is_running(self):
        return self.process is not None and self.process.returncode is None

    async def start(self):
        if self.is_running:
            return

        cmd = [
            sys.executable, "-m", "uvicorn",
            f"mcp_servers.{self.name}:{self.config.app_name}",
            "--host", "0.0.0.0",
            "--port", str(self.config.port)
        ]

        if self.config.use_https:
            if self.config.ssl_cert_path and self.config.ssl_key_path:
                cmd.extend(["--ssl-certfile", self.config.ssl_cert_path])
                cmd.extend(["--ssl-keyfile", self.config.ssl_key_path])
            else:
                self.log("Error: HTTPS selected but cert/key paths missing.")
                return

        self.log(f"Starting server with command: {' '.join(cmd)}")

        try:
            # Set cwd to current directory so python can find mcp_servers module
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd()
            )
            self.log(f"Server started with PID: {self.process.pid}")

            # Start background tasks to read logs
            asyncio.create_task(self._read_stream(self.process.stdout, "STDOUT"))
            asyncio.create_task(self._read_stream(self.process.stderr, "STDERR"))

            self.update_ui_state()

        except Exception as e:
            self.log(f"Failed to start server: {e}")

    async def stop(self):
        if not self.is_running:
            return

        self.log("Stopping server...")
        try:
            self.process.terminate()
            await self.process.wait()
            self.log("Server stopped.")
        except Exception as e:
            self.log(f"Error stopping server: {e}")
        finally:
            self.process = None
            self.update_ui_state()

    async def _read_stream(self, stream, prefix):
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded_line = line.decode().strip()
            if decoded_line:
                self.log(f"[{prefix}] {decoded_line}")

    def log(self, message: str):
        self.logs.append(message)
        if self.log_container:
            with self.log_container:
                ui.label(message).style('font-family: monospace; white-space: pre-wrap;')
            self.log_container.scroll_to(percent=1.0)

    def update_ui_state(self):
        if self.status_label:
            self.status_label.text = "Running" if self.is_running else "Stopped"
            self.status_label.classes(replace='text-green' if self.is_running else 'text-red')


servers: List[Server] = []

def scan_servers():
    global servers
    files = glob.glob("mcp_servers/*.py")
    current_filepaths = {s.filepath for s in servers}

    for f in files:
        if f not in current_filepaths:
            servers.append(Server(f))

scan_servers()

# -- UI --

@ui.page('/')
def main_page():
    ui.label('MCP Server Dashboard').classes('text-2xl font-bold q-mb-md')

    ui.button('Scan for Servers', on_click=lambda: (scan_servers(), server_list.refresh())).classes('q-mb-md')

    @ui.refreshable
    def server_list():
        if not servers:
            ui.label("No servers found in mcp_servers/").classes("text-gray-500 italic")
            return

        for server in servers:
            with ui.card().classes('w-full q-mb-md'):
                with ui.row().classes('items-center justify-between w-full'):
                    ui.label(server.name).classes('text-xl font-bold')
                    server.status_label = ui.label("Stopped").classes('text-red font-bold')

                with ui.expansion('Configuration', icon='settings').classes('w-full'):
                    with ui.column().classes('w-full'):
                        ui.input('App Instance Name', value=server.config.app_name, on_change=lambda e, s=server: setattr(s.config, 'app_name', e.value)).classes('w-full').props('placeholder="e.g. mcp"')
                        ui.number('Port', value=server.config.port, on_change=lambda e, s=server: setattr(s.config, 'port', int(e.value))).classes('w-full')

                        use_https = ui.checkbox('Use HTTPS', value=server.config.use_https, on_change=lambda e, s=server: setattr(s.config, 'use_https', e.value))

                        # Bind visibility to checkbox value
                        with ui.column().bind_visibility_from(use_https, 'value'):
                            ui.input('SSL Cert Path', value=server.config.ssl_cert_path, on_change=lambda e, s=server: setattr(s.config, 'ssl_cert_path', e.value)).classes('w-full')
                            ui.input('SSL Key Path', value=server.config.ssl_key_path, on_change=lambda e, s=server: setattr(s.config, 'ssl_key_path', e.value)).classes('w-full')

                with ui.row().classes('q-mt-md'):
                    ui.button('Start', on_click=server.start).props('color=green')
                    ui.button('Stop', on_click=server.stop).props('color=red')

                with ui.expansion('Logs', icon='article').classes('w-full q-mt-md'):
                    server.log_container = ui.scroll_area().classes('h-64 w-full bg-gray-100 p-2 border rounded')
                    # Render existing logs
                    with server.log_container:
                        for log in server.logs:
                            ui.label(log).style('font-family: monospace; white-space: pre-wrap;')

    server_list()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="MCP Dashboard", port=8080)
