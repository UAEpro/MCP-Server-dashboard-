import asyncio
import glob
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict
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

CONFIG_FILE = "mcp_servers/config.json"

def load_configs() -> Dict[str, dict]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_configs():
    data = {}
    for server in servers:
        data[server.name] = asdict(server.config)

    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

class Server:
    def __init__(self, filepath: str, saved_config: dict = None):
        self.filepath = filepath
        self.name = os.path.basename(filepath).replace(".py", "")
        if saved_config:
            self.config = ServerConfig(**saved_config)
        else:
            self.config = ServerConfig()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: List[str] = []
        self.log_container = None
        self.status_indicator = None
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
                ui.notify('HTTPS Error: Cert/Key paths missing', type='negative')
                return

        self.log(f"Starting server with command: {' '.join(cmd)}")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd()
            )
            self.log(f"Server started with PID: {self.process.pid}")

            asyncio.create_task(self._read_stream(self.process.stdout, "STDOUT"))
            asyncio.create_task(self._read_stream(self.process.stderr, "STDERR"))

            self.update_ui_state()
            ui.notify(f'{self.name} started', type='positive')

        except Exception as e:
            msg = f"Failed to start server: {e}"
            self.log(msg)
            ui.notify(msg, type='negative')

    async def stop(self):
        if not self.is_running:
            return

        self.log("Stopping server...")
        try:
            self.process.terminate()
            await self.process.wait()
            self.log("Server stopped.")
            ui.notify(f'{self.name} stopped', type='info')
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
            try:
                # Check if the container is still on a valid page/client
                if self.log_container.client.has_socket_connection:
                    with self.log_container:
                        # Terminal style log line
                        ui.label(message).style('font-family: "Fira Code", monospace; font-size: 0.85rem; line-height: 1.2;')
                    self.log_container.scroll_to(percent=1.0)
                else:
                    self.log_container = None
            except Exception:
                # If element is deleted or client disconnected, clear the reference
                self.log_container = None

    def update_ui_state(self):
        # Update the specific UI elements for this server
        if self.status_indicator:
            color = 'green' if self.is_running else 'grey'
            self.status_indicator.props(f'color={color}')

        if self.status_label:
            self.status_label.text = "RUNNING" if self.is_running else "STOPPED"
            self.status_label.classes(replace='text-green-500' if self.is_running else 'text-grey-500')


servers: List[Server] = []

def scan_servers():
    global servers
    files = glob.glob("mcp_servers/*.py")
    current_filepaths = {s.filepath for s in servers}
    saved_configs = load_configs()

    for f in files:
        if f not in current_filepaths:
            name = os.path.basename(f).replace(".py", "")
            servers.append(Server(f, saved_configs.get(name)))

scan_servers()

def create_new_server(name: str):
    if not name:
        ui.notify("Server name cannot be empty", type='negative')
        return

    filename = name.strip().replace(" ", "_").lower()
    if not filename.endswith(".py"):
        filename += ".py"

    filepath = os.path.join("mcp_servers", filename)
    if os.path.exists(filepath):
        ui.notify(f"Server {filename} already exists", type='negative')
        return

    content = f'''from fastmcp import FastMCP

# Create an MCP server
mcp = FastMCP("{name}")

@mcp.tool()
def hello_world() -> str:
    """A simple hello world tool"""
    return "Hello from {name}!"

if __name__ == "__main__":
    mcp.run()
'''
    try:
        with open(filepath, 'w') as f:
            f.write(content)
        ui.notify(f"Created {filename}", type='positive')
        scan_servers()
        server_grid.refresh()
    except Exception as e:
        ui.notify(f"Error creating file: {e}", type='negative')

# -- UI Components --

def layout():
    # Header
    with ui.header().classes('bg-slate-900 text-white shadow-lg items-center px-4 h-16'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('dns', size='md').classes('text-blue-400')
            ui.label('MCP Enterprise Manager').classes('text-xl font-semibold tracking-wide')

        ui.space()

        with ui.row().classes('items-center gap-4'):
             ui.button(icon='refresh', on_click=lambda: (scan_servers(), server_grid.refresh())).props('flat round dense color=white').tooltip('Rescan Servers')
             ui.button(icon='dark_mode', on_click=lambda: ui.dark_mode().toggle()).props('flat round dense color=white').tooltip('Toggle Dark Mode')
             ui.avatar(icon='person', color='blue-grey-8', text_color='white').props('size=sm')

    # Sidebar
    with ui.left_drawer(value=True).classes('bg-slate-50 border-r border-slate-200').props('width=240'):
        with ui.column().classes('w-full py-4'):
            def nav_item(label, icon, active=False):
                bg_color = 'bg-blue-50 text-blue-600' if active else 'text-slate-600 hover:bg-slate-100'
                with ui.row().classes(f'w-full items-center gap-4 px-6 py-3 cursor-pointer transition-colors {bg_color}'):
                    ui.icon(icon, size='sm')
                    ui.label(label).classes('font-medium')

            nav_item('Dashboard', 'dashboard', active=True)
            # nav_item('Configuration', 'settings') # Future global settings
            # nav_item('Logs', 'history') # Future global logs

            ui.separator().classes('my-4')

            with ui.row().classes('w-full px-6'):
                ui.label('SYSTEM STATUS').classes('text-xs font-bold text-slate-400 tracking-wider')
            with ui.row().classes('w-full px-6 py-2 items-center gap-2'):
                ui.element('div').classes('w-2 h-2 rounded-full bg-green-500')
                ui.label('System Online').classes('text-xs text-slate-500')


@ui.page('/')
def main_page():
    layout()

    with ui.column().classes('w-full p-8 bg-slate-100 min-h-screen gap-6'):
        # Page Title
        with ui.row().classes('w-full justify-between items-center'):
            with ui.column().classes('gap-1'):
                ui.label('Server Overview').classes('text-2xl font-bold text-slate-800')
                ui.label('Manage and monitor your MCP instances').classes('text-sm text-slate-500')

            ui.button('New Server', icon='add', color='blue-600', on_click=open_new_server_dialog).props('unelevated no-caps')

        # Server Grid
        server_grid()

def open_new_server_dialog():
    with ui.dialog() as dialog, ui.card().classes('w-96 p-6'):
        with ui.row().classes('w-full items-center justify-between q-mb-md'):
            ui.label('Create New Server').classes('text-xl font-bold text-slate-800')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=grey')

        name_input = ui.input('Server Name', placeholder='e.g. Weather Agent').classes('w-full').props('outlined dense autofocus')

        with ui.row().classes('w-full justify-end q-mt-lg'):
            ui.button('Create', on_click=lambda: (create_new_server(name_input.value), dialog.close()), color='blue-600').props('unelevated no-caps')

    dialog.open()

@ui.refreshable
def server_grid():
    if not servers:
        with ui.column().classes('w-full items-center justify-center py-20 text-slate-400 gap-4'):
            ui.icon('dns', size='4xl')
            ui.label('No servers found. Add files to mcp_servers/').classes('text-lg')
        return

    with ui.grid(columns=3).classes('w-full gap-6'):
        for server in servers:
            with ui.card().classes('w-full p-0 flex flex-col gap-0 shadow-sm border border-slate-200 hover:shadow-md transition-shadow duration-300'):
                # Card Header
                with ui.row().classes('w-full p-4 items-center justify-between border-b border-slate-100 bg-white'):
                    with ui.row().classes('items-center gap-3'):
                        ui.icon('terminal', color='blue-grey').classes('opacity-75')
                        ui.label(server.name).classes('font-bold text-slate-700 text-lg')

                    server.status_indicator = ui.icon('circle').props(f'color={"green" if server.is_running else "grey"} size=xs')

                # Card Body
                with ui.column().classes('p-4 gap-4 bg-white flex-grow'):
                    with ui.row().classes('w-full justify-between items-center text-sm'):
                        ui.label('Status').classes('text-slate-400 font-medium')
                        server.status_label = ui.label("RUNNING" if server.is_running else "STOPPED").classes(
                            'font-bold ' + ('text-green-500' if server.is_running else 'text-grey-500')
                        )

                    with ui.row().classes('w-full justify-between items-center text-sm'):
                        ui.label('Port').classes('text-slate-400 font-medium')
                        ui.label(str(server.config.port)).classes('text-slate-700 font-mono bg-slate-100 px-2 py-0.5 rounded')

                    with ui.row().classes('w-full justify-between items-center text-sm'):
                        ui.label('Protocol').classes('text-slate-400 font-medium')
                        proto = "HTTPS" if server.config.use_https else "HTTP"
                        ui.label(proto).classes('text-slate-700 font-mono bg-slate-100 px-2 py-0.5 rounded')

                    ui.separator().classes('my-1')

                    # Action Buttons
                    with ui.row().classes('w-full gap-2'):
                        start_btn = ui.button('Start', on_click=server.start, color='green-600' if not server.is_running else 'green-200').props('unelevated no-caps dense w-full').classes('flex-1')
                        start_btn.bind_enabled_from(server, 'is_running', backward=lambda x: not x)

                        stop_btn = ui.button('Stop', on_click=server.stop, color='red-600' if server.is_running else 'red-200').props('unelevated no-caps dense w-full').classes('flex-1')
                        stop_btn.bind_enabled_from(server, 'is_running')

                # Card Footer (Tools)
                with ui.row().classes('w-full p-2 bg-slate-50 border-t border-slate-100 justify-end gap-2'):
                    ui.button(icon='settings', on_click=lambda s=server: open_config_dialog(s)).props('flat round dense color=slate-500').tooltip('Configure')
                    ui.button(icon='article', on_click=lambda s=server: open_log_dialog(s)).props('flat round dense color=slate-500').tooltip('View Logs')


def open_config_dialog(server: Server):
    with ui.dialog() as dialog, ui.card().classes('w-96 p-6'):
        with ui.row().classes('w-full items-center justify-between q-mb-md'):
            ui.label(f'Configure {server.name}').classes('text-xl font-bold text-slate-800')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=grey')

        with ui.column().classes('w-full gap-4'):
            # Wrapper to auto-save on change
            def update_config(attr, value):
                setattr(server.config, attr, value)
                save_configs()

            ui.input('App Instance Name', value=server.config.app_name,
                     on_change=lambda e: update_config('app_name', e.value)).classes('w-full').props('outlined dense placeholder="e.g. mcp"')

            ui.number('Port', value=server.config.port,
                      on_change=lambda e: update_config('port', int(e.value))).classes('w-full').props('outlined dense')

            with ui.row().classes('items-center justify-between w-full border p-3 rounded border-slate-200'):
                ui.label('Enable HTTPS').classes('text-slate-700 font-medium')
                use_https = ui.switch('', value=server.config.use_https,
                                      on_change=lambda e: update_config('use_https', e.value))

            with ui.column().classes('w-full gap-2').bind_visibility_from(use_https, 'value'):
                ui.label('SSL Certificates').classes('text-xs font-bold text-slate-400 uppercase tracking-wider')
                ui.input('Cert Path', value=server.config.ssl_cert_path,
                         on_change=lambda e: update_config('ssl_cert_path', e.value)).classes('w-full').props('outlined dense')
                ui.input('Key Path', value=server.config.ssl_key_path,
                         on_change=lambda e: update_config('ssl_key_path', e.value)).classes('w-full').props('outlined dense')

        with ui.row().classes('w-full justify-end q-mt-lg'):
            ui.button('Done', on_click=dialog.close, color='blue-600').props('unelevated no-caps')

    dialog.open()

def open_log_dialog(server: Server):
    with ui.dialog() as dialog, ui.card().classes('w-[800px] h-[600px] p-0 flex flex-col overflow-hidden'):
        # Header
        with ui.row().classes('w-full items-center justify-between p-4 bg-slate-900 text-white'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('terminal', size='sm')
                ui.label(f'{server.name} - Console Output').classes('font-mono font-bold')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=white')

        # Log Area
        server.log_container = ui.scroll_area().classes('flex-grow bg-[#1e1e1e] p-4 w-full')

        # Render existing logs
        with server.log_container:
            for log in server.logs:
                ui.label(log).style('font-family: "Fira Code", monospace; font-size: 0.85rem; line-height: 1.2; color: #d4d4d4;')

    dialog.open()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="MCP Enterprise Manager", port=8080)
