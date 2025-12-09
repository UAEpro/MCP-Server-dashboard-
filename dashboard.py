import asyncio
import glob
import json
import os
import sys
import psutil
from dataclasses import dataclass, asdict, field
from typing import List, Optional, Dict
from nicegui import ui, app
from mcp import ClientSession
from mcp.client.sse import sse_client

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
    cwd: str = "" # Custom working directory
    source_type: str = "local" # 'local' (mcp_servers/) or 'imported'
    env: Dict[str, str] = field(default_factory=dict)
    auto_restart: bool = False

CONFIG_FILE = "mcp_servers/config.json"
LOG_DIR = "logs"

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

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
        # Update config with current path in case it changed (re-import?)
        # For now, just save the config object
        data[server.name] = asdict(server.config)
        # We also need to save the filepath for imported servers so we can restore them
        data[server.name]['filepath'] = server.filepath

    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

class Server:
    def __init__(self, filepath: str, saved_config: dict = None):
        self.filepath = filepath
        self.name = os.path.basename(filepath).replace(".py", "")

        if saved_config:
            # Handle migration/extra fields safely
            # Pop 'filepath' if it exists in saved_config to avoid __init__ error if not in ServerConfig
            if 'filepath' in saved_config:
                saved_config.pop('filepath')
            self.config = ServerConfig(**saved_config)
        else:
            self.config = ServerConfig()
            # Default CWD logic
            if self.config.source_type == 'local':
                self.config.cwd = os.getcwd()
            else:
                self.config.cwd = os.path.dirname(filepath)

        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: List[str] = []
        self.log_container = None
        self.status_indicator = None
        self.status_label = None

        # UI Elements references for updates
        self.start_btn = None
        self.stop_btn = None

        # Metrics
        self.cpu_history: List[float] = []
        self.mem_history: List[float] = []
        self.metrics_chart = None

    @property
    def is_running(self):
        return self.process is not None and self.process.returncode is None

    async def check_health(self):
        if self.process:
            if self.process.returncode is None:
                try:
                    # Check if actually alive by sending signal 0 (no-op)
                    self.process.send_signal(0)
                except ProcessLookupError:
                    self.log("Process died unexpectedly.")
                    # It's dead, let's treat it as closed
                    self.process = None

            # Check if we need to restart
            # If self.process is None (we cleared it above or previously) OR it has a returncode (stopped normally/crashed)
            if (self.process is None or self.process.returncode is not None) and self.config.auto_restart:
                 self.log(f"Auto-restarting server...")
                 self.process = None # Ensure clean state
                 await self.start()

    async def update_metrics(self):
        if not self.is_running or not self.process:
            return

        try:
            # pid property is available on asyncio subprocess
            pid = self.process.pid
            proc = psutil.Process(pid)

            # cpu_percent needs interval=None to be non-blocking (returns diff since last call)
            # But first call returns 0.0. We call it repeatedly every 2s.
            cpu = proc.cpu_percent(interval=None)
            mem_info = proc.memory_info()
            mem_mb = mem_info.rss / 1024 / 1024

            self.cpu_history.append(cpu)
            self.mem_history.append(mem_mb)

            # Keep last 60 points (2 minutes)
            if len(self.cpu_history) > 60:
                self.cpu_history.pop(0)
                self.mem_history.pop(0)

            if self.metrics_chart:
                 self.metrics_chart.options['series'][0]['data'] = self.cpu_history
                 self.metrics_chart.options['series'][1]['data'] = self.mem_history
                 self.metrics_chart.update()

        except Exception:
            pass

    async def start(self):
        if self.is_running:
            return

        # Determine execution details
        cwd = self.config.cwd if self.config.cwd else os.getcwd()

        # Construct import path
        # If local in mcp_servers, we use module syntax: mcp_servers.name:app
        # If imported/external, we rely on having CWD set to the script's folder,
        # and then just importing "filename:app"

        if self.config.source_type == 'local':
             # It's in mcp_servers/, so we run from repo root
             import_str = f"mcp_servers.{self.name}:{self.config.app_name}"
             exec_cwd = os.getcwd()
        else:
             # It's external. We set CWD to the file's directory.
             # Import string is just the filename without .py
             filename_no_ext = os.path.basename(self.filepath).replace(".py", "")
             import_str = f"{filename_no_ext}:{self.config.app_name}"
             exec_cwd = cwd

        cmd = [
            sys.executable, "-m", "uvicorn",
            import_str,
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
        self.log(f"Working Directory: {exec_cwd}")

        # Prepare environment variables
        env = os.environ.copy()
        env.update(self.config.env)

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=exec_cwd,
                env=env
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

        # Write to file
        try:
            log_file = os.path.join(LOG_DIR, f"{self.name}.log")
            with open(log_file, "a") as f:
                f.write(f"{message}\n")
        except Exception:
            pass # Don't crash on logging fail

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
        running = self.is_running

        if self.status_indicator:
            color = 'green' if running else 'grey'
            self.status_indicator.props(f'color={color}')

        if self.status_label:
            self.status_label.text = "RUNNING" if running else "STOPPED"
            self.status_label.classes(replace='text-green-500' if running else 'text-grey-500')

        if self.start_btn:
            if running:
                self.start_btn.disable()
            else:
                self.start_btn.enable()

        if self.stop_btn:
            if running:
                self.stop_btn.enable()
            else:
                self.stop_btn.disable()


servers: List[Server] = []

def scan_servers():
    global servers
    saved_configs = load_configs()

    # Track which servers we've already loaded/found
    processed_names = set()
    current_server_map = {s.name: s for s in servers}
    new_servers = []

    # 1. Process explicit/imported servers from config
    for name, config in saved_configs.items():
        if 'filepath' in config and config.get('source_type') == 'imported':
            processed_names.add(name)
            filepath = config['filepath']
            if name in current_server_map:
                continue # Already loaded

            # Verify file exists
            if os.path.exists(filepath):
                 new_servers.append(Server(filepath, config))
            else:
                print(f"Warning: Imported server {name} not found at {filepath}")

    # 2. Scan local mcp_servers/ directory
    files = glob.glob("mcp_servers/*.py")
    for f in files:
        name = os.path.basename(f).replace(".py", "")
        if name in processed_names:
            continue # Already handled (unlikely unless config overlaps)

        if name in current_server_map:
            continue # Already loaded

        # It's a local server
        config = saved_configs.get(name, {})
        config['source_type'] = 'local'
        new_servers.append(Server(f, config))

    servers.extend(new_servers)

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
            def nav_item(label, link, icon, active=False):
                bg_color = 'bg-blue-50 text-blue-600' if active else 'text-slate-600 hover:bg-slate-100'
                with ui.row().classes(f'w-full items-center gap-4 px-6 py-3 cursor-pointer transition-colors {bg_color}').on('click', lambda: ui.open(link)):
                    ui.icon(icon, size='sm')
                    ui.label(label).classes('font-medium')

            nav_item('Dashboard', '/', icon='dashboard', active=app.storage.user.get('page') == '/')
            nav_item('Settings', '/settings', icon='settings', active=app.storage.user.get('page') == '/settings')
            nav_item('Logs', '/logs', icon='history', active=app.storage.user.get('page') == '/logs')

            ui.separator().classes('my-4')

            with ui.row().classes('w-full px-6'):
                ui.label('SYSTEM STATUS').classes('text-xs font-bold text-slate-400 tracking-wider')
            with ui.row().classes('w-full px-6 py-2 items-center gap-2'):
                ui.element('div').classes('w-2 h-2 rounded-full bg-green-500')
                ui.label('System Online').classes('text-xs text-slate-500')


@ui.page('/')
def main_page():
    app.storage.user['page'] = '/'
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

def import_server(path: str, app_name: str):
    if not path or not os.path.exists(path):
        ui.notify("File path does not exist", type='negative')
        return

    name = os.path.basename(path).replace(".py", "")

    # Check for duplicates
    for s in servers:
        if s.name == name:
            ui.notify(f"Server with name {name} already exists", type='negative')
            return

    # Create config for imported server
    new_server = Server(path)
    new_server.config.app_name = app_name
    new_server.config.source_type = 'imported'
    new_server.config.cwd = os.path.dirname(path)

    servers.append(new_server)
    save_configs()
    server_grid.refresh()
    ui.notify(f"Imported {name}", type='positive')

def open_new_server_dialog():
    with ui.dialog() as dialog, ui.card().classes('w-[500px] p-6'):
        with ui.row().classes('w-full items-center justify-between q-mb-md'):
            ui.label('Add Server').classes('text-xl font-bold text-slate-800')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=grey')

        with ui.tabs().classes('w-full text-blue-600') as tabs:
            create_tab = ui.tab('Create New')
            import_tab = ui.tab('Import Existing')

        with ui.tab_panels(tabs, value=create_tab).classes('w-full'):
            with ui.tab_panel(create_tab):
                ui.label('Create a new FastMCP server in mcp_servers/').classes('text-sm text-slate-500 q-mb-md')
                name_input = ui.input('Server Name', placeholder='e.g. Weather Agent').classes('w-full').props('outlined dense autofocus')
                with ui.row().classes('w-full justify-end q-mt-lg'):
                    ui.button('Create', on_click=lambda: (create_new_server(name_input.value), dialog.close()), color='blue-600').props('unelevated no-caps')

            with ui.tab_panel(import_tab):
                ui.label('Import an existing Python script from anywhere').classes('text-sm text-slate-500 q-mb-md')
                path_input = ui.input('Script Path', placeholder='/path/to/main.py').classes('w-full').props('outlined dense')
                app_input = ui.input('App Instance Name', value='mcp', placeholder='e.g. mcp').classes('w-full').props('outlined dense')

                with ui.row().classes('w-full justify-end q-mt-lg'):
                    ui.button('Import', on_click=lambda: (import_server(path_input.value, app_input.value), dialog.close()), color='blue-600').props('unelevated no-caps')

    dialog.open()

@ui.page('/logs')
def logs_page():
    app.storage.user['page'] = '/logs'
    layout()

    with ui.column().classes('w-full p-8 bg-slate-100 min-h-screen gap-6'):
        ui.label('Server Logs').classes('text-2xl font-bold text-slate-800')

        # Log File Selection
        log_files = glob.glob(os.path.join(LOG_DIR, "*.log"))
        log_files_bases = [os.path.basename(f) for f in log_files]

        if not log_files:
            ui.label("No log files found.").classes('text-slate-500 italic')

        else:
            selected_log = ui.select(log_files_bases, label='Select Log File', value=log_files_bases[0] if log_files_bases else None).classes('w-64')

            log_view = ui.scroll_area().classes('w-full flex-grow bg-slate-900 text-slate-300 p-4 font-mono h-[600px] rounded')

            def refresh_log():
                log_view.clear()
                if not selected_log.value:
                    with log_view:
                        ui.label("No log file selected.")
                    return

                filepath = os.path.join(LOG_DIR, selected_log.value)
                if os.path.exists(filepath):
                    with open(filepath, 'r') as f:
                        content = f.read()
                        with log_view:
                            ui.label(content).style('white-space: pre-wrap;')
                else:
                    with log_view:
                        ui.label("File not found.")

            selected_log.on_value_change(refresh_log)
            ui.button('Refresh', on_click=refresh_log).props('icon=refresh')

            # Initial load
            refresh_log()

@ui.page('/settings')
def settings_page():
    app.storage.user['page'] = '/settings'
    layout()

    # Ensure servers are loaded
    if not servers:
        scan_servers()

    with ui.column().classes('w-full p-8 bg-slate-100 min-h-screen gap-6'):
        ui.label('Configuration').classes('text-2xl font-bold text-slate-800')

        with ui.card().classes('w-full p-6'):
            ui.label('Tracked Servers').classes('text-lg font-bold q-mb-md')

            if not servers:
                 ui.label("No servers tracked.").classes('text-slate-500 italic')
            else:
                # Simple table or list
                for server in servers:
                    with ui.row().classes('w-full items-center justify-between p-2 border-b border-slate-100'):
                        with ui.column().classes('gap-0'):
                            ui.label(server.name).classes('font-bold')
                            ui.label(server.filepath).classes('text-xs text-slate-500')

                        with ui.row().classes('items-center gap-4'):
                            ui.label(f"Port: {server.config.port}").classes('text-sm')
                            if server.config.source_type == 'imported':
                                ui.chip('Imported', color='blue-100', text_color='blue-800').props('dense')
                                # Ability to remove imported servers
                                ui.button(icon='delete', color='red', on_click=lambda s=server: remove_server(s)).props('flat round dense').tooltip('Forget Server')
                            else:
                                ui.chip('Local', color='green-100', text_color='green-800').props('dense')

def remove_server(server: Server):
    if server.is_running:
        ui.notify("Stop server before removing", type='negative')
        return

    if server in servers:
        servers.remove(server)
        save_configs() # This will remove it from config file since it iterates 'servers'
        ui.notify(f"Removed {server.name}", type='positive')
        ui.open('/settings') # Refresh page

async def monitor_servers():
    while True:
        for server in servers:
            try:
                await server.check_health()
                await server.update_metrics()
            except Exception as e:
                print(f"Error monitoring {server.name}: {e}")
        await asyncio.sleep(2.0)

app.on_startup(monitor_servers)

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
                        server.start_btn = ui.button('Start', on_click=server.start, color='green-600').props('unelevated no-caps dense w-full').classes('flex-1')
                        if server.is_running:
                            server.start_btn.disable()

                        server.stop_btn = ui.button('Stop', on_click=server.stop, color='red-600').props('unelevated no-caps dense w-full').classes('flex-1')
                        if not server.is_running:
                            server.stop_btn.disable()

                # Card Footer (Tools)
                with ui.row().classes('w-full p-2 bg-slate-50 border-t border-slate-100 justify-end gap-2'):
                    ui.button(icon='bug_report', on_click=lambda s=server: open_inspector_dialog(s)).props('flat round dense color=slate-500').tooltip('Inspector')
                    ui.button(icon='show_chart', on_click=lambda s=server: open_metrics_dialog(s)).props('flat round dense color=slate-500').tooltip('Metrics')
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

            with ui.row().classes('items-center justify-between w-full border p-3 rounded border-slate-200'):
                ui.label('Auto Restart').classes('text-slate-700 font-medium')
                ui.switch('', value=server.config.auto_restart,
                          on_change=lambda e: update_config('auto_restart', e.value))

            with ui.column().classes('w-full gap-2').bind_visibility_from(use_https, 'value'):
                ui.label('SSL Certificates').classes('text-xs font-bold text-slate-400 uppercase tracking-wider')
                ui.input('Cert Path', value=server.config.ssl_cert_path,
                         on_change=lambda e: update_config('ssl_cert_path', e.value)).classes('w-full').props('outlined dense')
                ui.input('Key Path', value=server.config.ssl_key_path,
                         on_change=lambda e: update_config('ssl_key_path', e.value)).classes('w-full').props('outlined dense')

            ui.separator().classes('my-2')
            ui.label('Environment Variables').classes('text-lg font-bold text-slate-800')

            # Env var editor
            env_container = ui.column().classes('w-full gap-2')

            def refresh_env_list():
                env_container.clear()
                with env_container:
                    if not server.config.env:
                        ui.label('No environment variables set.').classes('text-slate-500 italic text-sm')

                    for key, val in server.config.env.items():
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label(f"{key}").classes('font-mono font-bold text-slate-700')
                            ui.label('=').classes('text-slate-400')
                            ui.label(f"{val}").classes('font-mono text-slate-600 truncate flex-grow')
                            ui.button(icon='delete', on_click=lambda k=key: remove_env(k)).props('flat round dense color=red size=sm')

            def add_env(key, val):
                if not key: return
                server.config.env[key] = val
                save_configs()
                refresh_env_list()
                new_key.value = ''
                new_val.value = ''

            def remove_env(key):
                if key in server.config.env:
                    del server.config.env[key]
                    save_configs()
                    refresh_env_list()

            refresh_env_list()

            with ui.row().classes('w-full items-center gap-2'):
                new_key = ui.input(placeholder='KEY').props('outlined dense').classes('flex-1')
                new_val = ui.input(placeholder='VALUE').props('outlined dense').classes('flex-1')
                ui.button(icon='add', on_click=lambda: add_env(new_key.value, new_val.value)).props('flat round dense color=green')

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

async def open_inspector_dialog(server: Server):
    with ui.dialog() as dialog, ui.card().classes('w-[900px] h-[700px] p-0 flex flex-col'):
        # Header
        with ui.row().classes('w-full items-center justify-between p-4 bg-slate-900 text-white'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('bug_report', size='sm')
                ui.label(f'{server.name} - Inspector').classes('font-bold')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=white')

        content_area = ui.column().classes('w-full flex-grow p-6 overflow-auto gap-6')

        if not server.is_running:
            with content_area:
                 ui.label("Server must be running to inspect.").classes("text-red-500 italic")
            dialog.open()
            return

        with content_area:
            ui.spinner(size='lg').classes('self-center')

        dialog.open()

        # Async load tools
        try:
            protocol = "https" if server.config.use_https else "http"
            url = f"{protocol}://localhost:{server.config.port}/sse"

            # Note: If self-signed certs are used, sse_client might raise SSL errors.
            # Currently mcp.client.sse doesn't easily expose ssl_context overrides.
            async with sse_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    tools = tools_result.tools

                    content_area.clear()
                    with content_area:
                        if not tools:
                            ui.label("No tools found.").classes("text-slate-500 italic")

                        for tool in tools:
                            with ui.card().classes('w-full border border-slate-200 p-4'):
                                ui.label(tool.name).classes('text-lg font-bold text-slate-800')
                                ui.label(tool.description).classes('text-sm text-slate-500 q-mb-md')

                                # Inputs for arguments
                                args_inputs = {}
                                if tool.inputSchema and 'properties' in tool.inputSchema:
                                    with ui.grid(columns=2).classes('w-full gap-4 q-mb-md'):
                                        for prop_name, prop_schema in tool.inputSchema['properties'].items():
                                            prop_type = prop_schema.get('type', 'string')
                                            label = f"{prop_name} ({prop_type})"
                                            if prop_type == 'integer' or prop_type == 'number':
                                                args_inputs[prop_name] = ui.number(label).props('outlined dense')
                                            else:
                                                args_inputs[prop_name] = ui.input(label).props('outlined dense')

                                result_area = ui.label('').classes('whitespace-pre-wrap font-mono bg-slate-100 p-2 rounded text-xs hidden w-full')

                                async def run_tool(t_name=tool.name, t_inputs=args_inputs, r_area=result_area):
                                    # Collect args
                                    args = {}
                                    for k, v in t_inputs.items():
                                        if v.value is not None and v.value != '':
                                            args[k] = v.value

                                    r_area.classes(remove='hidden')
                                    r_area.text = "Running..."

                                    try:
                                        # Re-connect for execution
                                        async with sse_client(url) as (read_exec, write_exec):
                                            async with ClientSession(read_exec, write_exec) as exec_session:
                                                await exec_session.initialize()
                                                res = await exec_session.call_tool(t_name, arguments=args)

                                                # Parse result (it's a CallToolResult object)
                                                output_text = ""
                                                for content in res.content:
                                                    if content.type == 'text':
                                                        output_text += content.text
                                                    else:
                                                        output_text += f"[{content.type} content]"

                                                r_area.text = output_text
                                                if res.isError:
                                                    r_area.classes('text-red-600')
                                                else:
                                                    r_area.classes('text-slate-700')

                                    except Exception as e:
                                        r_area.text = f"Error: {e}"
                                        r_area.classes('text-red-600')

                                ui.button('Run Tool', on_click=run_tool).props('unelevated no-caps color=blue-600')

                                # Re-add result area to layout so it appears below button
                                result_area.move(result_area.parent_slot)

        except Exception as e:
            content_area.clear()
            with content_area:
                 ui.label(f"Failed to connect to inspector: {e}").classes("text-red-500")

def open_metrics_dialog(server: Server):
    with ui.dialog() as dialog, ui.card().classes('w-[600px] p-6'):
        with ui.row().classes('w-full items-center justify-between q-mb-md'):
            ui.label(f'{server.name} - Live Metrics').classes('text-xl font-bold text-slate-800')
            ui.button(icon='close', on_click=dialog.close).props('flat round dense color=grey')

        if not server.is_running:
            ui.label("Server is not running.").classes("text-slate-500 italic")
        else:
            server.metrics_chart = ui.echart({
                'tooltip': {'trigger': 'axis'},
                'legend': {'data': ['CPU %', 'Memory (MB)']},
                'xAxis': {'type': 'category', 'boundaryGap': False, 'data': list(range(60))},
                'yAxis': [
                    {'type': 'value', 'name': 'CPU %', 'min': 0, 'max': 100},
                    {'type': 'value', 'name': 'Memory (MB)', 'min': 0}
                ],
                'series': [
                    {'name': 'CPU %', 'type': 'line', 'data': server.cpu_history, 'smooth': True, 'showSymbol': False},
                    {'name': 'Memory (MB)', 'type': 'line', 'yAxisIndex': 1, 'data': server.mem_history, 'smooth': True, 'showSymbol': False}
                ]
            }).classes('w-full h-64')

    dialog.open()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="MCP Enterprise Manager", port=8080, storage_secret='mcp-dashboard-secret')
