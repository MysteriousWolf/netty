# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

netty is a Serial/TCP relay tool that bridges physical serial ports with TCP connections and creates virtual serial ports. It operates in two modes:

- **Server mode**: Connects to a physical serial port and exposes it over TCP
- **Client mode**: Connects to a TCP server and creates a virtual serial port locally

The tool uses a custom framing protocol to transmit both data and modem control signals (RTS, CTS, DTR, DSR, CD, RI) over TCP.

## Development Commands

### Installation and Setup

```bash
# Install using uv (recommended)
uv sync

# Install in development mode
uv pip install -e .
```

### Running the Application

```bash
# Run directly
python main.py <port> <tcp_port> [options]

# Or use the installed command
netty <port> <tcp_port> [options]

# Server mode examples
netty /dev/ttyUSB0 8888
netty COM3 8888 --baud 9600 -v

# Client mode examples (auto-detected when --host is specified)
netty auto 8888 --host 192.168.1.100
netty COM5 8888 --host server.local -v
```

### Testing

No test suite is currently present in the repository.

## Architecture

### Core Components

**SerialRelay class** (main.py:100): The central orchestrator that manages:
- Serial port connections (physical or virtual)
- TCP server/client sockets
- PTY (pseudo-terminal) file descriptors on Unix
- Bidirectional data relay threads
- Signal handling and cleanup

### Communication Protocol

The application uses a binary framing protocol for TCP communication:

**Frame structure**: `[msg_type:1 byte][length:2 bytes][data:variable]`

**Message types** (main.py:42-48):
- `MSG_DATA (0x01)`: Serial data payload
- `MSG_RTS (0x02)`: Request To Send signal
- `MSG_CTS (0x03)`: Clear To Send signal
- `MSG_DTR (0x04)`: Data Terminal Ready signal
- `MSG_DSR (0x05)`: Data Set Ready signal
- `MSG_CD (0x06)`: Carrier Detect signal
- `MSG_RI (0x07)`: Ring Indicator signal

Helper functions:
- `pack_message()` (main.py:72): Encodes messages
- `unpack_message()` (main.py:76): Decodes messages from socket

### Threading Model

Each mode spawns two daemon threads for bidirectional data relay:

**Server mode**:
- `server_serial_to_tcp` (main.py:296): Reads from physical serial, forwards to TCP
- `server_tcp_to_serial` (main.py:357): Reads from TCP, forwards to serial

**Client mode** (platform-specific):
- Unix: `client_pty_to_tcp` (main.py:530) and `client_tcp_to_pty` (main.py:628)
- Windows: `client_serial_to_tcp` (main.py:564) and `client_tcp_to_serial` (main.py:584)

All threads use 100ms timeouts for responsive shutdown on Ctrl+C.

### Platform-Specific Behavior

**Unix/Linux** (main.py:476):
- Uses PTY (pseudo-terminal) pairs via `pty.openpty()`
- Creates symlinks in `/tmp/` for user-friendly port names
- Sets raw mode with `tty.setraw()`

**Windows** (main.py:422):
- Requires com0com virtual serial port driver
- Auto-detects CNCA/CNCB port pairs
- Falls back to regular COM ports if com0com not found

### Signal Handling

The application propagates modem control signals bidirectionally:
- Server reads output signals (CTS, DSR, CD, RI) from physical port
- Both ends can set input signals (RTS, DTR)
- Signal changes are detected and transmitted as separate messages

## Important Implementation Details

### Mode Detection

Mode is auto-detected based on command-line arguments (main.py:775):
- **Server mode**: No `--host` specified or `--server` flag present
- **Client mode**: `--host` specified

### Error Handling

- Serial port errors are caught and logged, allowing brief recovery periods
- TCP connection failures trigger thread shutdown and cleanup
- Signal read failures (especially on certain platforms) are logged but non-fatal
- All cleanup is performed in `SerialRelay.cleanup()` (main.py:131)

### Verbose Logging

When `-v` flag is enabled:
- TX/RX data is logged in hex format (first 32 bytes)
- Signal state changes are logged
- Debug messages show internal state transitions
- Uses color-coded output via ANSI escape codes (main.py:51-59)

## Python Version

The project requires Python >= 3.14 (specified in pyproject.toml:6).

## Dependencies

- `pyserial`: Serial port communication
- `argcomplete`: Optional tab completion support (disabled by default at main.py:23)
