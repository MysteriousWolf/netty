#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""
netty - Serial/TCP Relay with Virtual Serial Port Support
"""
import sys
import socket
import serial
import serial.tools.list_ports
import struct
import signal
import select
import argparse
import threading
import time
import os
from pathlib import Path
from typing import Optional
import platform
import traceback

# Configuration
ENABLE_ARGCOMPLETE = False  # Set to True to enable tab completion

# Platform-specific imports
IS_WINDOWS = platform.system() == 'Windows'

if not IS_WINDOWS:
    import pty
    import tty

# Only import argcomplete if enabled
HAS_ARGCOMPLETE = False
if ENABLE_ARGCOMPLETE:
    try:
        import argcomplete
        HAS_ARGCOMPLETE = True
    except ImportError:
        HAS_ARGCOMPLETE = False

# Message types for framing protocol
MSG_DATA = 0x01
MSG_RTS = 0x02
MSG_CTS = 0x03
MSG_DTR = 0x04
MSG_DSR = 0x05
MSG_CD = 0x06
MSG_RI = 0x07

# ANSI color codes
class Colors:
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'

def log(message: str, color: str = ''):
    """Print colored log message"""
    print(f"{color}{message}{Colors.RESET}", flush=True)

def log_verbose(prefix: str, data: bytes, color: str):
    """Log TX/RX data in verbose mode"""
    hex_str = ' '.join(f'{b:02X}' for b in data[:32])
    if len(data) > 32:
        hex_str += f' ... ({len(data)} bytes)'
    log(f"{prefix}: {hex_str}", color)

def pack_message(msg_type: int, data: bytes = b'') -> bytes:
    """Pack message with type, length, and data"""
    return struct.pack('!BH', msg_type, len(data)) + data

def unpack_message(sock: socket.socket) -> tuple[int, bytes]:
    """Unpack message from socket"""
    header = sock.recv(3)
    if len(header) < 3:
        raise ConnectionError("Connection closed")
    msg_type, length = struct.unpack('!BH', header)
    data = b''
    if length > 0:
        data = sock.recv(length)
        if len(data) < length:
            raise ConnectionError("Incomplete message")
    return msg_type, data

def list_serial_ports():
    """List all available serial ports"""
    ports = serial.tools.list_ports.comports()
    return [port.device for port in ports]

def port_completer(prefix, parsed_args, **kwargs):
    """Autocomplete function for serial ports"""
    ports = list_serial_ports()
    ports.append('auto')
    return ports

class SerialRelay:
    def __init__(self, args):
        if args.verbose:
            log(f"[DEBUG] SerialRelay.__init__: Starting initialization", Colors.GRAY)
        
        self.args = args
        self.running = True
        self.ser: Optional[serial.Serial] = None
        self.client_sock: Optional[socket.socket] = None
        self.server_sock: Optional[socket.socket] = None
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.virtual_port: Optional[str] = None
        self.user_port: Optional[str] = None
        self.threads = []
        
        if args.verbose:
            log(f"[DEBUG] SerialRelay.__init__: Setting up signal handlers", Colors.GRAY)
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        if args.verbose:
            log(f"[DEBUG] SerialRelay.__init__: Initialization complete", Colors.GRAY)

    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        log("\n[SIGNAL] Shutting down gracefully...", Colors.YELLOW)
        self.running = False

    def cleanup(self):
        """Clean up all resources"""
        if self.args.verbose:
            log(f"[DEBUG] cleanup: Starting cleanup", Colors.GRAY)
        
        self.running = False
        
        # Wait for threads to finish
        if self.args.verbose and self.threads:
            log(f"[DEBUG] cleanup: Waiting for {len(self.threads)} threads to finish", Colors.GRAY)
        
        for thread in self.threads:
            if thread.is_alive():
                if self.args.verbose:
                    log(f"[DEBUG] cleanup: Waiting for thread {thread.name}", Colors.GRAY)
                thread.join(timeout=1.0)
        
        # Close sockets
        if self.args.verbose:
            log(f"[DEBUG] cleanup: Closing sockets", Colors.GRAY)
        
        for sock in [self.client_sock, self.server_sock]:
            if sock:
                try:
                    sock.close()
                    if self.args.verbose:
                        log(f"[DEBUG] cleanup: Socket closed", Colors.GRAY)
                except Exception as e:
                    if self.args.verbose:
                        log(f"[DEBUG] cleanup: Error closing socket: {e}", Colors.GRAY)
        
        # Close serial port
        if self.ser:
            try:
                if self.args.verbose:
                    log(f"[DEBUG] cleanup: Closing serial port", Colors.GRAY)
                if self.ser.is_open:
                    self.ser.close()
                if self.args.verbose:
                    log(f"[DEBUG] cleanup: Serial port closed", Colors.GRAY)
            except Exception as e:
                if self.args.verbose:
                    log(f"[DEBUG] cleanup: Error closing serial: {e}", Colors.GRAY)
        
        # Close file descriptors
        if self.args.verbose and (self.master_fd or self.slave_fd):
            log(f"[DEBUG] cleanup: Closing file descriptors", Colors.GRAY)
        
        for fd in [self.master_fd, self.slave_fd]:
            if fd:
                try:
                    os.close(fd)
                    if self.args.verbose:
                        log(f"[DEBUG] cleanup: FD {fd} closed", Colors.GRAY)
                except Exception as e:
                    if self.args.verbose:
                        log(f"[DEBUG] cleanup: Error closing FD {fd}: {e}", Colors.GRAY)
        
        log("[INFO] Cleanup complete", Colors.GREEN)

    def run_server(self):
        """Run in server mode"""
        try:
            if self.args.verbose:
                log(f"[DEBUG] run_server: Starting server mode", Colors.GRAY)
                log(f"[DEBUG] run_server: Port={self.args.port}, Baud={self.args.baud}", Colors.GRAY)
            
            # Open serial port
            log(f"[SERVER] Opening serial port {self.args.port} @ {self.args.baud}...", Colors.CYAN)
            
            try:
                self.ser = serial.Serial(
                    port=self.args.port,
                    baudrate=self.args.baud,
                    timeout=0.1
                )
                log(f"[SERVER] Serial port opened successfully", Colors.GREEN)
            except Exception as e:
                log(f"[ERROR] Failed to open serial port: {e}", Colors.RED)
                raise
            
            if self.args.verbose:
                log(f"[DEBUG] Serial port info: {self.ser}", Colors.GRAY)
            
            # Create TCP server
            if self.args.verbose:
                log(f"[DEBUG] Creating TCP server socket...", Colors.GRAY)
            
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind(('0.0.0.0', self.args.tcp_port))
            self.server_sock.listen(1)
            self.server_sock.settimeout(1.0)
            
            log(f"[SERVER] Listening on TCP port {self.args.tcp_port}", Colors.GREEN)
            
            if self.args.verbose:
                log(f"[DEBUG] Entering main server loop...", Colors.GRAY)
            
            while self.running:
                try:
                    if self.args.verbose:
                        log(f"[DEBUG] Waiting for client connection...", Colors.GRAY)
                    
                    client_sock, addr = self.server_sock.accept()
                    log(f"[SERVER] Client connected from {addr[0]}:{addr[1]}", Colors.GREEN)
                    self.client_sock = client_sock
                    self.client_sock.settimeout(0.1)
                    
                    if self.args.verbose:
                        log(f"[DEBUG] Starting connection handler...", Colors.GRAY)
                    
                    self.handle_server_connection()
                    
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        log(f"[ERROR] Connection error: {e}", Colors.RED)
                        if self.args.verbose:
                            traceback.print_exc()
                finally:
                    if self.client_sock:
                        try:
                            self.client_sock.close()
                        except:
                            pass
                        self.client_sock = None
                        log("[SERVER] Client disconnected", Colors.YELLOW)
        
        except serial.SerialException as e:
            log(f"[ERROR] Serial port error: {e}", Colors.RED)
            if self.args.verbose:
                traceback.print_exc()
        except Exception as e:
            log(f"[ERROR] Server error: {e}", Colors.RED)
            if self.args.verbose:
                traceback.print_exc()
        finally:
            if self.args.verbose:
                log(f"[DEBUG] run_server: Cleaning up...", Colors.GRAY)
            self.cleanup()

    def handle_server_connection(self):
        """Handle data relay between serial and TCP"""
        self.threads = [
            threading.Thread(target=self.server_serial_to_tcp, daemon=True),
            threading.Thread(target=self.server_tcp_to_serial, daemon=True)
        ]
        
        for t in self.threads:
            t.start()
        
        # Wait until threads exit, non-blocking so Ctrl+C works
        try:
            while self.running and any(t.is_alive() for t in self.threads):
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.running = False

        # Graceful join
        for t in self.threads:
            t.join(timeout=1.0)


    def server_serial_to_tcp(self):
        """Forward data from serial port to TCP connection"""
        last_signals = {}
        try:
            while self.running and self.client_sock:
                # Read serial data
                try:
                    data = self.ser.read(self.ser.in_waiting or 1)
                except serial.SerialException as e:
                    log(f"[ERROR] Serial read failed: {e}", Colors.RED)
                    time.sleep(0.1)
                    continue

                # Forward serial data to TCP
                if data:
                    msg = pack_message(MSG_DATA, data)
                    try:
                        self.client_sock.sendall(msg)
                        if self.args.verbose:
                            log_verbose("[TCP TX]", data, Colors.MAGENTA)
                    except Exception as e:
                        if self.running:
                            log(f"[ERROR] TCP send failed: {e}", Colors.RED)
                        break

                # Read modem control signals safely
                signals = {}
                try:
                    signals = {
                        MSG_CTS: bool(self.ser.cts),
                        MSG_DSR: bool(self.ser.dsr),
                        MSG_CD:  bool(self.ser.cd),
                        MSG_RI:  bool(self.ser.ri)
                    }
                except (serial.SerialException, PermissionError, OSError) as e:
                    if self.args.verbose:
                        log(f"[WARN] reading serial signals failed: {e}", Colors.YELLOW)
                    time.sleep(0.05)
                    continue

                # Send signal changes
                for sig_type, value in signals.items():
                    if sig_type not in last_signals or last_signals[sig_type] != value:
                        try:
                            msg = pack_message(sig_type, struct.pack('?', value))
                            self.client_sock.sendall(msg)
                            last_signals[sig_type] = value
                            if self.args.verbose:
                                sig_name = {MSG_CTS: 'CTS', MSG_DSR: 'DSR', MSG_CD: 'CD', MSG_RI: 'RI'}[sig_type]
                                log(f"[SIGNAL] {sig_name} = {value}", Colors.GRAY)
                        except Exception as e:
                            if self.args.verbose:
                                log(f"[WARN] failed to send signal msg: {e}", Colors.YELLOW)

                time.sleep(0.01)

        except Exception as e:
            if self.running:
                log(f"[ERROR] server_serial_to_tcp crashed: {e}", Colors.RED)


    def server_tcp_to_serial(self):
        """Forward data from TCP to serial"""
        try:
            while self.running and self.client_sock and self.ser:
                try:
                    msg_type, data = unpack_message(self.client_sock)
                    
                    if msg_type == MSG_DATA:
                        if self.args.verbose:
                            log_verbose("[TCP RX]", data, Colors.CYAN)
                        self.ser.write(data)
                        if self.args.verbose:
                            log_verbose("[SERIAL TX]", data, Colors.GREEN)
                    
                    elif msg_type == MSG_RTS:
                        value = struct.unpack('?', data)[0]
                        self.ser.rts = value
                        if self.args.verbose:
                            log(f"[SIGNAL] RTS = {value}", Colors.GRAY)
                    
                    elif msg_type == MSG_DTR:
                        value = struct.unpack('?', data)[0]
                        self.ser.dtr = value
                        if self.args.verbose:
                            log(f"[SIGNAL] DTR = {value}", Colors.GRAY)
                
                except socket.timeout:
                    continue
                except ConnectionError:
                    break
                except Exception as e:
                    if self.running:
                        log(f"[ERROR] TCP->Serial error: {e}", Colors.RED)
                    break
        
        except Exception as e:
            if self.running:
                log(f"[ERROR] TCP receive error: {e}", Colors.RED)

    def run_client(self):
        """Run in client mode"""
        try:
            if IS_WINDOWS:
                self._setup_windows_client()
            else:
                self._setup_unix_client()
            
            # Connect to TCP server
            log(f"[CLIENT] Connecting to {self.args.tcp_host}:{self.args.tcp_port}...", Colors.CYAN)
            self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_sock.connect((self.args.tcp_host, self.args.tcp_port))
            self.client_sock.settimeout(0.1)
            log(f"[CLIENT] Connected to server", Colors.GREEN)
            
            self.handle_client_connection()
        
        except ConnectionRefusedError:
            log(f"[ERROR] Connection refused. Is the server running?", Colors.RED)
        except Exception as e:
            log(f"[ERROR] Client error: {e}", Colors.RED)
            if self.args.verbose:
                traceback.print_exc()
        finally:
            self.cleanup()

    def _setup_windows_client(self):
        """Setup Windows virtual serial port using com0com"""
        log("[CLIENT] Setting up virtual serial port (Windows)...", Colors.CYAN)
        
        available_ports = list_serial_ports()
        
        if self.args.port and self.args.port != 'auto':
            requested_port = self.args.port.upper()
            if not requested_port.startswith('COM'):
                requested_port = 'COM' + requested_port
            
            # Find paired port
            if 'CNCA' in requested_port:
                user_port = requested_port.replace('CNCA', 'CNCB')
                relay_port = requested_port
            elif 'CNCB' in requested_port:
                user_port = requested_port
                relay_port = requested_port.replace('CNCB', 'CNCA')
            else:
                relay_port = requested_port
                user_port = requested_port
                log(f"[WARN] Using regular COM port {requested_port}", Colors.YELLOW)
            
            self.virtual_port = relay_port
            self.user_port = user_port
        else:
            # Auto mode - find first com0com pair
            cnca_ports = [p for p in available_ports if 'CNCA' in p.upper()]
            
            if cnca_ports:
                self.virtual_port = cnca_ports[0]
                port_num = ''.join(filter(str.isdigit, self.virtual_port))
                self.user_port = f"CNCB{port_num}"
                log(f"[CLIENT] Found com0com pair: {self.virtual_port} <-> {self.user_port}", Colors.GREEN)
            else:
                log("[ERROR] No com0com virtual ports found!", Colors.RED)
                log("[INFO] Please install com0com and create a port pair:", Colors.YELLOW)
                log("[INFO]   1. Download from: https://sourceforge.net/projects/com0com/", Colors.YELLOW)
                log("[INFO]   2. Install and open 'Setup Command Prompt'", Colors.YELLOW)
                log("[INFO]   3. Run: install PortName=CNCA0 PortName=CNCB0", Colors.YELLOW)
                log("[INFO]   4. Netty will use CNCA0, your app uses CNCB0", Colors.YELLOW)
                sys.exit(1)
        
        log(f"{Colors.CYAN}[CLIENT] Netty relay port: {Colors.BLUE}{self.virtual_port}{Colors.RESET}", '')
        log(f"{Colors.CYAN}[CLIENT] Connect your application to: {Colors.GREEN}{self.user_port}{Colors.RESET}", '')
        
        # Open relay port
        log(f"[CLIENT] Opening relay port {self.virtual_port}...", Colors.CYAN)
        self.ser = serial.Serial(
            port=self.virtual_port,
            baudrate=115200,
            timeout=0.1
        )

    def _setup_unix_client(self):
        """Setup Unix/Linux virtual serial port using PTY"""
        log("[CLIENT] Creating virtual serial port (Linux/Unix)...", Colors.CYAN)
        self.master_fd, self.slave_fd = pty.openpty()
        
        # Get slave port name
        slave_name = os.ttyname(self.slave_fd)
        self.virtual_port = slave_name
        
        # Try to create symlink if requested
        if self.args.port and self.args.port != 'auto':
            try:
                link_path = f"/tmp/{os.path.basename(self.args.port)}"
                if os.path.exists(link_path):
                    os.unlink(link_path)
                os.symlink(slave_name, link_path)
                self.virtual_port = link_path
                log(f"[CLIENT] Created symlink: {link_path} -> {slave_name}", Colors.GREEN)
            except Exception as e:
                log(f"[WARN] Could not create symlink: {e}", Colors.YELLOW)
        
        log(f"[CLIENT] Virtual serial port: {Colors.GREEN}{self.virtual_port}{Colors.RESET}", Colors.CYAN)
        log(f"[CLIENT] Connect your application to: {Colors.GREEN}{self.virtual_port}{Colors.RESET}", Colors.YELLOW)
        
        # Set raw mode
        tty.setraw(self.master_fd)

    def handle_client_connection(self):
        """Handle data relay between TCP and virtual serial"""
        if IS_WINDOWS:
            self.threads = [
                threading.Thread(target=self.client_tcp_to_serial, daemon=True),
                threading.Thread(target=self.client_serial_to_tcp, daemon=True)
            ]
        else:
            self.threads = [
                threading.Thread(target=self.client_tcp_to_pty, daemon=True),
                threading.Thread(target=self.client_pty_to_tcp, daemon=True)
            ]
        
        for t in self.threads:
            t.start()

        # Wait until threads exit, non-blocking so Ctrl+C works
        try:
            while self.running and any(t.is_alive() for t in self.threads):
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.running = False

        # Graceful join
        for t in self.threads:
            t.join(timeout=1.0)

    def client_pty_to_tcp(self):
        """Forward data from PTY to TCP (non-blocking, safe exit on Ctrl+C)"""
        try:
            while self.running and self.client_sock:
                # Wait for PTY input with timeout
                rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
                if not rlist:
                    continue

                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    continue

                if not data:
                    continue

                if self.args.verbose:
                    log_verbose("[PTY RX]", data, Colors.BLUE)

                msg = pack_message(MSG_DATA, data)
                try:
                    self.client_sock.sendall(msg)
                    if self.args.verbose:
                        log_verbose("[TCP TX]", data, Colors.MAGENTA)
                except Exception as e:
                    if self.running:
                        log(f"[ERROR] TCP sendall failed: {e}", Colors.RED)
                    break

        except Exception as e:
            if self.running:
                log(f"[ERROR] PTY->TCP thread error: {e}", Colors.RED)

    def client_serial_to_tcp(self):
        """Forward data from serial to TCP (Windows)"""
        try:
            while self.running and self.client_sock and self.ser:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    if data:
                        if self.args.verbose:
                            log_verbose("[SERIAL RX]", data, Colors.BLUE)
                        msg = pack_message(MSG_DATA, data)
                        self.client_sock.sendall(msg)
                        if self.args.verbose:
                            log_verbose("[TCP TX]", data, Colors.MAGENTA)
                else:
                    time.sleep(0.001)
        
        except Exception as e:
            if self.running:
                log(f"[ERROR] Serial->TCP error: {e}", Colors.RED)

    def client_tcp_to_serial(self):
        """Forward data from TCP to serial (Windows)"""
        try:
            while self.running and self.client_sock and self.ser:
                try:
                    msg_type, data = unpack_message(self.client_sock)
                    
                    if msg_type == MSG_DATA:
                        if self.args.verbose:
                            log_verbose("[TCP RX]", data, Colors.CYAN)
                        self.ser.write(data)
                        if self.args.verbose:
                            log_verbose("[SERIAL TX]", data, Colors.GREEN)
                    
                    elif msg_type in (MSG_CTS, MSG_DSR, MSG_CD, MSG_RI, MSG_RTS, MSG_DTR):
                        if self.args.verbose:
                            sig_name = {
                                MSG_CTS: 'CTS', MSG_DSR: 'DSR', 
                                MSG_CD: 'CD', MSG_RI: 'RI',
                                MSG_RTS: 'RTS', MSG_DTR: 'DTR'
                            }[msg_type]
                            value = struct.unpack('?', data)[0]
                            log(f"[SIGNAL] {sig_name} = {value}", Colors.GRAY)
                            
                            # Apply control signals if possible
                            if msg_type == MSG_RTS:
                                self.ser.rts = value
                            elif msg_type == MSG_DTR:
                                self.ser.dtr = value
                
                except socket.timeout:
                    continue
                except ConnectionError:
                    log("[ERROR] Server disconnected", Colors.RED)
                    break
                except Exception as e:
                    if self.running:
                        log(f"[ERROR] TCP->Serial error: {e}", Colors.RED)
                    break
        
        except Exception as e:
            if self.running:
                log(f"[ERROR] TCP receive error: {e}", Colors.RED)

    def client_tcp_to_pty(self):
        """Forward data from TCP to PTY"""
        try:
            while self.running and self.client_sock:
                try:
                    msg_type, data = unpack_message(self.client_sock)
                    
                    if msg_type == MSG_DATA:
                        if self.args.verbose:
                            log_verbose("[TCP RX]", data, Colors.CYAN)
                        os.write(self.master_fd, data)
                        if self.args.verbose:
                            log_verbose("[PTY TX]", data, Colors.GREEN)
                    
                    elif msg_type in (MSG_CTS, MSG_DSR, MSG_CD, MSG_RI, MSG_RTS, MSG_DTR):
                        if self.args.verbose:
                            sig_name = {
                                MSG_CTS: 'CTS', MSG_DSR: 'DSR', 
                                MSG_CD: 'CD', MSG_RI: 'RI',
                                MSG_RTS: 'RTS', MSG_DTR: 'DTR'
                            }[msg_type]
                            value = struct.unpack('?', data)[0]
                            log(f"[SIGNAL] {sig_name} = {value}", Colors.GRAY)
                
                except socket.timeout:
                    continue
                except ConnectionError:
                    log("[ERROR] Server disconnected", Colors.RED)
                    break
                except Exception as e:
                    if self.running:
                        log(f"[ERROR] TCP->PTY error: {e}", Colors.RED)
                    break
        
        except Exception as e:
            if self.running:
                log(f"[ERROR] TCP receive error: {e}", Colors.RED)

def main():
    # Print IMMEDIATELY to confirm script is running
    print("[STARTUP] netty starting...", flush=True)
    
    parser = argparse.ArgumentParser(
        description='Serial/TCP Relay with Virtual Serial Port Support',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Server mode (auto-detected when no --host):
    netty /dev/ttyUSB0 8888
    netty COM3 8888 --baud 9600 -v
  
  Client mode (auto-detected when --host specified):
    netty auto 8888 --host 192.168.1.100
    netty COM5 8888 --host server.local -v
        """
    )
    
    print("[STARTUP] Parser created", flush=True)
    
    parser.add_argument(
        'port',
        nargs='?',
        default='auto',
        help='Serial port (server) or virtual port name (client, default: auto)'
    ).completer = port_completer
    
    parser.add_argument(
        'tcp_port',
        type=int,
        help='TCP port number'
    )
    
    parser.add_argument(
        '--baud',
        type=int,
        default=115200,
        help='Baud rate (default: 115200, server only)'
    )
    
    parser.add_argument(
        '--host',
        dest='tcp_host',
        default=None,
        help='TCP host to connect to (specifying this enables client mode)'
    )
    
    parser.add_argument(
        '--server',
        action='store_true',
        help='Force server mode (optional, auto-detected if --host not specified)'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose TX/RX logging'
    )
    
    print("[STARTUP] Arguments defined", flush=True)
    
    # Enable argcomplete if available and enabled
    if ENABLE_ARGCOMPLETE and HAS_ARGCOMPLETE:
        print("[STARTUP] Argcomplete is enabled, checking for completion mode...", flush=True)
        try:
            # Check if we're in completion mode
            if '_ARGCOMPLETE' in os.environ:
                print("[STARTUP] Argcomplete mode detected, running autocomplete", flush=True)
                argcomplete.autocomplete(parser)
            else:
                print("[STARTUP] Normal mode, skipping argcomplete", flush=True)
        except SystemExit as e:
            print(f"[STARTUP] argcomplete caused SystemExit: {e}", flush=True)
            raise
        except Exception as e:
            print(f"[STARTUP] argcomplete error (non-fatal): {e}", flush=True)
            traceback.print_exc()
    else:
        if not ENABLE_ARGCOMPLETE:
            print("[STARTUP] Argcomplete disabled (ENABLE_ARGCOMPLETE=False)", flush=True)
        else:
            print("[STARTUP] Argcomplete not available (not installed)", flush=True)
    
    print("[STARTUP] Parsing arguments...", flush=True)
    
    try:
        args = parser.parse_args()
        print(f"[STARTUP] Arguments parsed successfully", flush=True)
    except SystemExit as e:
        print(f"[STARTUP] parse_args caused SystemExit with code: {e.code}", flush=True)
        # Re-raise SystemExit to allow --help to work
        raise
    except Exception as e:
        log(f"[ERROR] Failed to parse arguments: {e}", Colors.RED)
        traceback.print_exc()
        sys.exit(1)
    
    # Debug logging
    if args.verbose:
        log(f"[DEBUG] Arguments parsed:", Colors.GRAY)
        log(f"[DEBUG]   port: {args.port}", Colors.GRAY)
        log(f"[DEBUG]   tcp_port: {args.tcp_port}", Colors.GRAY)
        log(f"[DEBUG]   baud: {args.baud}", Colors.GRAY)
        log(f"[DEBUG]   tcp_host: {args.tcp_host}", Colors.GRAY)
        log(f"[DEBUG]   server: {args.server}", Colors.GRAY)
        log(f"[DEBUG]   verbose: {args.verbose}", Colors.GRAY)
    
    # Auto-detect mode based on --host
    is_server = args.server or (args.tcp_host is None)
    
    if args.verbose:
        log(f"[DEBUG] Mode detection: is_server={is_server}", Colors.GRAY)
    
    # Set default host for client mode
    if not is_server and args.tcp_host is None:
        args.tcp_host = 'localhost'
        if args.verbose:
            log(f"[DEBUG] Set default tcp_host to localhost", Colors.GRAY)
    
    # Validate server mode requires port
    if is_server and args.port == 'auto':
        log("[ERROR] Server mode requires a serial port to be specified", Colors.RED)
        sys.exit(1)
    
    # Log mode
    mode = "SERVER" if is_server else "CLIENT"
    log(f"[{mode}] Starting netty in {mode.lower()} mode", Colors.CYAN)
    
    if args.verbose:
        log(f"[DEBUG] Creating SerialRelay instance...", Colors.GRAY)
    
    try:
        relay = SerialRelay(args)
        
        if args.verbose:
            log(f"[DEBUG] SerialRelay instance created", Colors.GRAY)
            log(f"[DEBUG] Starting {'server' if is_server else 'client'} mode...", Colors.GRAY)
        
        if is_server:
            relay.run_server()
        else:
            relay.run_client()
            
    except KeyboardInterrupt:
        log("\n[INFO] Interrupted by user", Colors.YELLOW)
        if 'relay' in locals():
            relay.cleanup()
    except Exception as e:
        log(f"[ERROR] Fatal error: {e}", Colors.RED)
        traceback.print_exc()
        if 'relay' in locals():
            relay.cleanup()
        sys.exit(1)

if __name__ == '__main__':
    try:
        print("[ENTRY] Script starting, calling main()...", flush=True)
        main()
        print("[EXIT] main() completed normally", flush=True)
    except SystemExit as e:
        print(f"[EXIT] SystemExit with code: {e.code}", flush=True)
        raise
    except Exception as e:
        print(f"[FATAL] Unhandled exception in main: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
