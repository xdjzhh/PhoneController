"""
Virtual Display implementation using scrcpy command line.
Simple wrapper around scrcpy executable.
"""

import subprocess
import os
import signal
import atexit
import shutil
import threading
import time
import adbutils


class VirtualDisplay:
    """
    Virtual Display using scrcpy command line.
    """
    
    def __init__(self, device_serial=None, max_size=0, max_fps=60, 
                 bitrate='8M', no_control=False, stay_awake=False,
                 turn_screen_off=False, record=None, scrcpy_path=None,
                 start_app=None):
        """
        Initialize virtual display.
        
        Args:
            device_serial: ADB device serial number (None for auto-detect)
            max_size: Maximum display size (0 = no limit)
            max_fps: Maximum frames per second
            bitrate: Video bitrate (e.g., '8M', '4M')
            no_control: Disable touch control
            stay_awake: Keep screen awake during capture
            turn_screen_off: Turn off device screen during capture
            record: Record to file (e.g., 'output.mp4')
            scrcpy_path: Path to scrcpy executable (default: auto-detect)
            start_app: Package name of app to launch (None for home screen)
        """
        self.device_serial = device_serial
        self.max_size = max_size
        self.max_fps = max_fps
        self.bitrate = bitrate
        self.no_control = no_control
        self.stay_awake = stay_awake
        self.turn_screen_off = turn_screen_off
        self.record = record
        self.start_app = start_app
        
        # Find scrcpy executable
        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        
        self.process = None
        self.running = False
        self.monitor_thread = None
        self.stop_monitoring = False
        
        # Cleanup on exit
        atexit.register(self.stop)
    
    def _find_scrcpy(self):
        """Find scrcpy executable path."""
        # Try to find in PATH
        scrcpy = shutil.which("scrcpy")
        if scrcpy:
            return scrcpy
        
        # Try common locations
        common_paths = [
            "D:/scrcpy-win64-v4.1/scrcpy.exe",
            "C:/Program Files/scrcpy/scrcpy.exe",
            "C:/Program Files (x86)/scrcpy/scrcpy.exe",
            os.path.expanduser("~/AppData/Local/Programs/scrcpy/scrcpy.exe"),
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                return path
        
        raise FileNotFoundError(
            "scrcpy not found. Please install scrcpy or provide scrcpy_path parameter."
        )
    
    def start(self):
        """Start scrcpy."""
        if self.running:
            print("Virtual display already running")
            return
        
        # Build scrcpy command
        cmd = [self.scrcpy_path]
        
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])
        
        if self.max_size > 0:
            cmd.extend(["-m", str(self.max_size)])
        
        cmd.extend(["--max-fps", str(self.max_fps)])
        cmd.extend(["--video-bit-rate", self.bitrate])
        
        if self.no_control:
            cmd.append("--no-control")
        
        if self.stay_awake:
            cmd.append("--stay-awake")
        
        if self.turn_screen_off:
            cmd.append("--turn-screen-off")
        
        if self.record:
            cmd.extend(["--record", self.record])
        
        print(f"Starting scrcpy: {' '.join(cmd)}")
        
        # Start scrcpy process
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        self.running = True
        print(f"Virtual display started (PID: {self.process.pid})")
        
        # Launch app if specified
        if self.start_app:
            self._launch_app()
            # Start monitoring thread to check if app is still in foreground
            self._start_app_monitor()
    
    def _launch_app(self):
        """Launch the specified app on device."""
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if devices:
                    device = devices[0]
                else:
                    print("No device found, cannot launch app")
                    return
            
            # Launch app using monkey command (more reliable than am start)
            device.shell(f"monkey -p {self.start_app} -c android.intent.category.LAUNCHER 1")
            print(f"Launched app: {self.start_app}")
        except Exception as e:
            print(f"Failed to launch app {self.start_app}: {e}")
    
    def _start_app_monitor(self):
        """Start monitoring thread to check if app is still in foreground."""
        self.stop_monitoring = False
        self.monitor_thread = threading.Thread(target=self._monitor_app, daemon=True)
        self.monitor_thread.start()
        print(f"Started monitoring app: {self.start_app}")
    
    def _monitor_app(self):
        """Monitor if the specified app is still in foreground."""
        if self.device_serial:
            device = adbutils.adb.device(self.device_serial)
        else:
            devices = adbutils.adb.device_list()
            if devices:
                device = devices[0]
            else:
                return
        
        while not self.stop_monitoring and self.running:
            try:
                # Get current foreground app package name
                result = device.shell("dumpsys activity activities | grep mResumedActivity")
                
                # Parse package name from result like: mResumedActivity: ActivityRecord{... com.taobao.taobao/...}
                if self.start_app and self.start_app not in result:
                    print(f"App {self.start_app} is no longer in foreground, closing virtual display...")
                    self.stop()
                    break
                
                time.sleep(1)  # Check every second
            except Exception as e:
                print(f"Monitor error: {e}")
                time.sleep(1)
    
    def stop(self):
        """Stop scrcpy and cleanup."""
        if not self.running:
            return
        
        self.running = False
        self.stop_monitoring = True
        print("Stopping virtual display...")
        
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"Error stopping scrcpy: {e}")
        
        # Cleanup scrcpy-server.jar from device
        self._cleanup_server()
        
        print("Virtual display stopped")
    
    def _cleanup_server(self):
        """Cleanup scrcpy-server.jar from device."""
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if devices:
                    device = devices[0]
                else:
                    return
            
            # Remove scrcpy-server.jar
            device.shell("rm -f /data/local/tmp/scrcpy-server.jar")
            print("Cleaned up scrcpy-server.jar")
        except Exception as e:
            print(f"Cleanup warning: {e}")
    
    def wait(self):
        """Wait for scrcpy to exit."""
        if self.process:
            self.process.wait()
            self.running = False


def start_virtual_display(device_serial=None, max_size=0, max_fps=60,
                          bitrate='8M', no_control=False, stay_awake=False,
                          turn_screen_off=False, record=None, start_app=None):
    """
    Start virtual display using scrcpy.
    
    Args:
        device_serial: ADB device serial number
        max_size: Maximum display size (0 = no limit)
        max_fps: Maximum frames per second
        bitrate: Video bitrate
        no_control: Disable touch control
        stay_awake: Keep screen awake
        turn_screen_off: Turn off device screen
        record: Record to file
        start_app: Package name of app to launch (None for home screen)
    """
    display = VirtualDisplay(
        device_serial=device_serial,
        max_size=max_size,
        max_fps=max_fps,
        bitrate=bitrate,
        no_control=no_control,
        stay_awake=stay_awake,
        turn_screen_off=turn_screen_off,
        record=record,
        start_app=start_app
    )
    
    try:
        display.start()
        display.wait()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        display.stop()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Virtual Display using scrcpy")
    parser.add_argument("--serial", "-s", type=str, help="Device serial number")
    parser.add_argument("--max-size", "-m", type=int, default=0, help="Maximum display size (0=no limit)")
    parser.add_argument("--max-fps", type=int, default=60, help="Maximum frames per second")
    parser.add_argument("--bitrate", type=str, default='8M', help="Video bitrate (e.g., 8M, 4M)")
    parser.add_argument("--no-control", action='store_true', help="Disable touch control")
    parser.add_argument("--stay-awake", action='store_true', help="Keep screen awake")
    parser.add_argument("--turn-screen-off", action='store_true', help="Turn off device screen")
    parser.add_argument("--record", type=str, help="Record to file (e.g., output.mp4)")
    parser.add_argument("--start-app", type=str, help="Package name of app to launch (empty for home screen)")
    
    args = parser.parse_args()
    
    start_virtual_display(
        device_serial=args.serial,
        max_size=args.max_size,
        max_fps=args.max_fps,
        bitrate=args.bitrate,
        no_control=args.no_control,
        stay_awake=args.stay_awake,
        turn_screen_off=args.turn_screen_off,
        record=args.record,
        start_app=args.start_app
    )
