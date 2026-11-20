"""Thin Picamera2 adapter. Hardware acquisition is blocked unless explicitly enabled."""
class PicameraCapture:
    def __init__(self, c):
        from picamera2 import Picamera2
        self.fps = c['capture_fps']
        self.camera = Picamera2()
        # Picamera2 RGB888 is BGR byte order on this platform, matching OpenCV.
        profile = self.camera.create_video_configuration(
            main={'size': (c['width'], c['height']), 'format': 'RGB888'},
            controls={'FrameRate': self.fps}, buffer_count=2, queue=False)
        self.camera.configure(profile)
        self.camera.start()

    def isOpened(self):
        return True

    def set(self, *_):
        return False  # Parameters were supplied to the mature capture backend.

    def read(self):
        return True, self.camera.capture_array('main')

    def release(self):
        self.camera.stop()
        self.camera.close()
