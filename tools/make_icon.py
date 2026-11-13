"""Generates assets/icon.ico: a Currents-style psychedelic swirl with a download arrow."""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

N = 512
y, x = np.mgrid[0:N, 0:N]
cx = cy = N / 2
dx, dy = x - cx, y - cy
r = np.hypot(dx, dy) / (N / 2)
a = np.arctan2(dy, dx)
t = (a / (2 * np.pi) + r * 1.6) % 1.0          # twisting phase
stops = np.array([[255, 61, 154], [255, 138, 31], [255, 210, 63], [138, 61, 255], [31, 209, 200], [255, 61, 154]], float)
pos = t * (len(stops) - 1)
i = np.clip(pos.astype(int), 0, len(stops) - 2)
f = (pos - i)[..., None]
rgb = stops[i] * (1 - f) + stops[i + 1] * f
ring = 0.82 + 0.18 * np.cos(r * 60)               # concentric ripples
rgb = rgb * ring[..., None]
img = Image.fromarray(rgb.clip(0, 255).astype('uint8'), 'RGB').convert('RGBA')

mask = Image.new('L', (N, N), 0)                  # rounded-square app shape
ImageDraw.Draw(mask).rounded_rectangle([8, 8, N - 8, N - 8], radius=110, fill=255)
img.putalpha(mask)

glow = Image.new('RGBA', (N, N), (0, 0, 0, 0))    # soft dark disc behind the arrow
d = ImageDraw.Draw(glow)
d.ellipse([110, 110, N - 110, N - 110], fill=(34, 9, 77, 190))
img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(8)))

d = ImageDraw.Draw(img)                           # download arrow
w = (255, 243, 224, 255)
d.rounded_rectangle([226, 150, 286, 290], radius=18, fill=w)
d.polygon([(160, 270), (352, 270), (256, 372)], fill=w)
d.rounded_rectangle([160, 388, 352, 414], radius=13, fill=w)
img.save('assets/icon.png')
img.save('assets/icon.ico', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print('icon ok')
