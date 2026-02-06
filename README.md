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

## ⚠️ Safety and Liability Disclaimer

**READ THIS CAREFULLY BEFORE USING THIS SOFTWARE**

This software is provided for controlling CNC machines and other potentially dangerous equipment. By using this software, you acknowledge and agree to the following:

### No Warranty

THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. THE SOFTWARE MAY CONTAIN BUGS, ERRORS, OR DEFECTS THAT COULD CAUSE EQUIPMENT MALFUNCTION, PROPERTY DAMAGE, PERSONAL INJURY, OR DEATH.

### No Liability

IN NO EVENT SHALL THE AUTHORS, COPYRIGHT HOLDERS, OR CONTRIBUTORS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE, INCLUDING BUT NOT LIMITED TO:

- Personal injury or loss of life
- Property damage or equipment damage
- Loss of data or work product
- Loss of business or profits
- Any direct, indirect, incidental, special, exemplary, or consequential damages

### User Responsibility

YOU ARE SOLELY RESPONSIBLE FOR:

1. **Safety Measures**: Implementing appropriate safety systems, emergency stops, interlocks, guards, and protective equipment according to applicable safety standards
2. **Testing and Validation**: Thoroughly testing this software in a safe environment before use on production equipment
3. **Supervision**: Never leaving CNC equipment running unattended
4. **Compliance**: Ensuring compliance with all applicable local, state, national, and international laws, regulations, and safety standards
5. **Professional Review**: Having this software reviewed by qualified engineers or safety professionals before use in any critical application
6. **Risk Assessment**: Understanding the risks involved in operating CNC machinery and taking appropriate precautions
7. **Maintenance**: Keeping all equipment properly maintained and in safe operating condition

### CNC-Specific Warnings

CNC machines are inherently dangerous. They can:
- Move rapidly and with great force
- Cause severe crushing injuries
- Generate projectiles from broken tools or workpieces
- Create fire hazards from spindle operation
- Cause electrical hazards
- Produce dangerous noise levels and particulates

**Do not operate CNC equipment unless you are trained and qualified to do so.**

### No Support Guarantee

The authors provide this software on a voluntary basis and are under no obligation to provide support, updates, bug fixes, or any assistance whatsoever.

### Assumption of Risk

BY USING THIS SOFTWARE, YOU EXPRESSLY ACKNOWLEDGE AND ASSUME ALL RISKS ASSOCIATED WITH ITS USE. If you do not agree to these terms, do not use this software.

---

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
    "rapid_override": 1.0,
    "max_velocity": 100.0,
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

#### Overrides
```json
{"cmd": "set_feed_override", "scale": 1.0}
{"cmd": "set_spindle_override", "scale": 1.0}
{"cmd": "set_rapid_override", "scale": 1.0}
{"cmd": "set_max_velocity", "velocity": 100.0}
```

**Scale ranges**:
- Feed: 0.0-2.0 (0-200%)
- Spindle: 0.5-2.0 (50-200%)
- Rapid: 0.0-1.0 (0-100%)

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

MIT License with Safety Disclaimer

Copyright (c) 2025 [Your Name/Organization]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.**

**THIS SOFTWARE IS INTENDED FOR USE WITH POTENTIALLY DANGEROUS EQUIPMENT.
BY USING THIS SOFTWARE, YOU ACCEPT ALL RISKS AND AGREE THAT THE AUTHORS
BEAR NO RESPONSIBILITY FOR ANY INJURIES, DEATHS, PROPERTY DAMAGE, OR OTHER
LOSSES THAT MAY RESULT FROM ITS USE.**

## Acknowledgments

Built on top of the excellent [LinuxCNC](https://linuxcnc.org) project.

---

**Note**: This is an independent project and is not officially affiliated with LinuxCNC.
