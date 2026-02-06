# LCNC Suite

A modern, UI-agnostic WebSocket gateway for LinuxCNC with a reference Vue 3 web interface.

## Overview

**lcnc-suite** provides a real-time WebSocket API for controlling and monitoring LinuxCNC machines. The architecture separates machine control (backend) from user interface (frontend), allowing you to build custom UIs for web, mobile, desktop, or embedded applications.

### Architecture

```
┌─────────────────────┐
│   Any UI Client     │  ← Web, Mobile, Desktop, Custom Hardware
│   (Your Choice)     │
└──────────┬──────────┘
           │ WebSocket (ws://host:8000/ws)
           │
┌──────────▼──────────┐
│   lcnc-gateway      │  ← UI-Agnostic API Server
│   (Python/FastAPI)  │
└──────────┬──────────┘
           │ Python API
           │
┌──────────▼──────────┐
│     LinuxCNC        │  ← Machine Controller
└─────────────────────┘
```

The repository includes:
- **lcnc-gateway**: Production-ready WebSocket gateway (UI-agnostic)
- **lcnc-webui**: Reference Vue 3 web interface (optional, one possible UI)

## Features

### Gateway (lcnc-gateway)

- **Real-time Status**: 10 Hz machine state updates via WebSocket
- **Full Machine Control**: E-stop, enable, jog, MDI, auto run
- **Safety System**: Connection-level arming to prevent accidental commands
- **3D Visualization Support**: Machine model, kinematics, G-code preview
- **Position Tracking**: Machine, work, and joint coordinates with full offset handling
- **Tool Management**: Tool number, diameter, and length offset tracking
- **G-code Parser**: Preview generator for feed and rapid moves (G0/G1/G2/G3)
- **Error Channel**: Real-time LinuxCNC error/message forwarding
- **STL Model Serving**: Static file server for 3D machine models

### Reference UI (lcnc-webui)

- Modern Vue 3 + TypeScript interface
- Real-time DRO (Digital Read Out)
- Manual jogging controls
- MDI command interface
- 3D machine visualization with Three.js
- Responsive design for tablets and desktop

## Requirements

### Gateway
- LinuxCNC 2.8+ with Python bindings
- Python 3.9+
- Virtual environment with system-site-packages enabled

### Reference UI
- Node.js 18+ and npm

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/lcnc-suite.git
cd lcnc-suite
```

### 2. Setup Gateway

```bash
cd lcnc-gateway
./setup-venv.sh  # Creates venv with system-site-packages for LinuxCNC
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Setup Web UI (Optional)

```bash
cd ../lcnc-webui
npm install
```

## Quick Start

### Using the Launcher Script

The easiest way to run both services:

```bash
# Localhost only (default)
./restart.sh local

# Accessible from LAN
./restart.sh lan
```

The script will:
- Stop any existing instances
- Start the gateway on port 8000
- Start the web UI on port 5173
- Perform health checks
- Open your browser automatically

Logs are saved to `runlogs/`

### Manual Start

**Gateway:**
```bash
cd lcnc-gateway
source .venv/bin/activate
uvicorn gateway:app --host 127.0.0.1 --port 8000
```

**Web UI:**
```bash
cd lcnc-webui
npm run dev -- --host 127.0.0.1 --port 5173
```

## WebSocket API

### Connection

```javascript
const ws = new WebSocket('ws://localhost:8000/ws');
```

### Server Messages

The gateway sends these message types:

#### `viewer_init`
Machine model configuration (sent once per connection):
```json
{
  "type": "viewer_init",
  "data": {
    "units": "mm",
    "stl_base_url": "http://localhost:8000/assets/",
    "machine_bounds": {
      "origin": [0, 0, 0],
      "size": [300, 200, 100]
    },
    "parts": [...],
    "kinematics": {...}
  }
}
```

#### `status`
Full machine status (10 Hz):
```json
{
  "type": "status",
  "data": {
    "ts": 1234567890.123,
    "estop": false,
    "enabled": true,
    "homed": true,
    "task_mode": 1,
    "interp_state": 1,
    "state": 1,
    "machine_pos": [10.5, 20.3, 5.0],
    "work_pos": [0.0, 0.0, 0.0],
    "joint_pos": [10.5, 20.3, 5.0],
    "g5x_offset": [10.5, 20.3, 5.0],
    "g92_offset": [0.0, 0.0, 0.0],
    "tool_offset": [0.0, 0.0, 0.0],
    "dtg": [0.0, 0.0, 0.0],
    "feed_override": 1.0,
    "spindle_override": 1.0,
    "active_file": "/path/to/file.ngc",
    "motion_line": 42,
    "tool_number": 1,
    "tool_diameter": 6.0,
    "tool_length": 50.0
  },
  "errors": []
}
```

#### `viewer_state`
Lightweight state for 3D rendering (10 Hz):
```json
{
  "type": "viewer_state",
  "data": {
    "ts": 1234567890.123,
    "machine_pos": [10.5, 20.3, 5.0],
    "joint_pos": [10.5, 20.3, 5.0],
    "work_pos": [0.0, 0.0, 0.0],
    "tool_number": 1,
    "tool_diameter": 6.0,
    "tool_length": 50.0
  }
}
```

#### `viewer_gcode`
G-code toolpath preview (sent when active file changes):
```json
{
  "type": "viewer_gcode",
  "data": {
    "file": "/path/to/file.ngc",
    "feed": [[x, y, z], ...],
    "rapid": [[x, y, z], ...]
  }
}
```

#### `reply`
Command response:
```json
{
  "type": "reply",
  "ok": true
}
```

### Client Commands

Send JSON messages to control the machine:

#### Arming
```json
{"cmd": "arm", "armed": true}
```

#### E-stop Control
```json
{"cmd": "estop"}
{"cmd": "estop_reset"}
```

#### Machine Power
```json
{"cmd": "machine_on"}
{"cmd": "machine_off"}
```

#### Abort
```json
{"cmd": "abort"}
```

#### MDI Commands
```json
{"cmd": "mdi", "text": "G0 X10 Y20"}
```

#### Auto Run
```json
{"cmd": "auto_run", "line": 0}
```

#### Jogging
```json
{"cmd": "jog_cont", "axis": 0, "vel": 100.0}
{"cmd": "jog_stop", "axis": 0}
```

**Axis mapping**: 0=X, 1=Y, 2=Z, etc.

### Error Handling

All commands return a reply:
```json
{
  "type": "reply",
  "ok": false,
  "error": "Cannot Machine On while in E-stop"
}
```

## Building Your Own UI

The gateway is completely UI-agnostic. To build your own interface:

1. **Connect to WebSocket**: `ws://<gateway-host>:8000/ws`
2. **Receive `viewer_init`**: Get machine configuration
3. **Receive `status` messages**: Update your UI @ 10 Hz
4. **Send commands**: Control the machine via JSON messages
5. **Handle replies**: Process command responses and errors

Example minimal client:
```javascript
const ws = new WebSocket('ws://localhost:8000/ws');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  switch(msg.type) {
    case 'viewer_init':
      console.log('Machine config:', msg.data);
      break;
    case 'status':
      console.log('Position:', msg.data.work_pos);
      break;
    case 'reply':
      console.log('Command result:', msg.ok);
      break;
  }
};

// Arm the connection
ws.send(JSON.stringify({cmd: 'arm', armed: true}));

// Send MDI command
ws.send(JSON.stringify({cmd: 'mdi', text: 'G0 X10'}));
```

## Development

### Gateway Development

```bash
cd lcnc-gateway
source .venv/bin/activate

# Run with auto-reload
uvicorn gateway:app --reload --host 127.0.0.1 --port 8000
```

### UI Development

```bash
cd lcnc-webui
npm run dev
```

The Vite dev server provides hot module replacement for rapid development.

## Configuration

### Machine Models

STL models are stored in `lcnc-gateway/machine/`:
- `frame.stl` - Machine base/frame
- `x_axis.stl` - X-axis assembly
- `y_axis.stl` - Y-axis assembly
- `z_axis.stl` - Z-axis assembly

Models are tracked with Git LFS (see `.gitattributes`).

### Kinematics

Edit the `build_viewer_init()` function in `gateway.py` to match your machine's kinematics and model transforms.

### Polling Rate

Adjust `POLL_HZ` in `gateway.py` (default: 10 Hz).

## Project Structure

```
lcnc-suite/
├── lcnc-gateway/          # Backend WebSocket gateway
│   ├── gateway.py         # FastAPI application
│   ├── requirements.txt   # Python dependencies
│   ├── setup-venv.sh      # Virtual environment setup
│   └── machine/           # STL machine models (Git LFS)
├── lcnc-webui/            # Reference Vue 3 UI
│   ├── src/
│   │   ├── App.vue
│   │   ├── lcncWs.ts      # WebSocket client
│   │   ├── ThreeViewer.vue
│   │   └── ...
│   └── package.json
├── restart.sh             # Production launcher
├── runlogs/               # Application logs
└── README.md
```

## Contributing

Contributions welcome! This project is designed to be extensible:

- **Gateway enhancements**: Add new commands, improve status data
- **Alternative UIs**: Build interfaces for different platforms
- **Machine models**: Contribute STL models for common machines
- **Documentation**: Improve API docs and examples

## License

[Add your license here]

## Acknowledgments

Built on top of the excellent [LinuxCNC](https://linuxcnc.org) project.

---

**Note**: This is an independent project and is not officially affiliated with LinuxCNC.
