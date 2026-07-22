"""Scrcpy client for displaying phone screen on computer using mysc-core."""

import time
import cv2
import numpy as np
import adbutils
from mysc_core.session import Session
from mysc_core.video import VideoKwargs


def get_screen_size(device) -> tuple[int, int]:
    """Get device screen resolution via ADB."""
    output = device.shell("wm size")
    # Output format: "Physical size: 1080x2400"
    for line in output.strip().split("\n"):
        if "Physical size" in line:
            size_str = line.split(":")[-1].strip()
            width, height = size_str.split("x")
            return int(width), int(height)
    return 1080, 2400  # Default fallback


def start_display(device_serial=None, max_fps=60):
    """
    Display phone screen on computer in real-time using mysc-core.

    Args:
        device_serial: ADB device serial number. None for auto-detect.
        max_fps: Maximum frames per second.
    """
    # Get ADB device
    if device_serial:
        device = adbutils.device(device_serial)
    else:
        device = adbutils.device()

    # Get actual screen size and use it as max_size
    screen_width, screen_height = get_screen_size(device)
    max_size = max(screen_width, screen_height)
    print(f"Connected to device: {device.serial}, screen: {screen_width}x{screen_height}, max_size: {max_size}")

    # Video configuration
    video_kwargs = VideoKwargs(
        video=True,
        max_fps=max_fps,
        max_size=max_size,
    )

    # Frame storage
    latest_frame = [None]
    frame_count = [0]

    def on_frame(frame_n, frame):
        """Callback to receive frames from scrcpy session. frame is av.VideoFrame."""
        try:
            ndarray = frame.to_ndarray(format='rgb24')
            latest_frame[0] = ndarray
            frame_count[0] += 1
            if frame_count[0] == 1:
                print(f"First frame received: frame_n={frame_n}, shape={ndarray.shape}, dtype={ndarray.dtype}")
        except Exception as e:
            if frame_count[0] == 0:
                print(f"Frame conversion error: {e}")

    # Create scrcpy session
    session = Session(
        device=device,
        video_kwargs=video_kwargs,
        frame_update_callback=on_frame
    )

    print("Displaying phone screen. Press 'q' to quit.")

    window_name = "Phone Screen"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            frame = latest_frame[0]
            if frame is not None:
                # Convert RGB to BGR for OpenCV display
                if len(frame.shape) == 3 and frame.shape[2] == 3:
                    display_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                else:
                    display_frame = frame
                cv2.imshow(window_name, display_frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

            time.sleep(0.001)  # Small delay to prevent CPU overload
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        session.disconnect()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    start_display()
