# Assembles renders/cat_<pose>.png into renders/cat_poses_sheet.png
from PIL import Image, ImageDraw, ImageFont
ps = ['default', 'sleep', 'sit', 'stretch', 'walk_1', 'walk_2']
f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 22)
s = Image.new('RGB', (2400, 1280), '#efe9df'); d = ImageDraw.Draw(s)
for i, p in enumerate(ps):
    x, y = (i % 3) * 800, (i // 3) * 640
    s.paste(Image.open('renders/cat_' + p + '.png').convert('RGB'), (x, y + 40))
    d.text((x + 14, y + 8), 'pet_cat (default)' if p == 'default' else 'pet_cat_pose_' + p, fill='#3a2a1e', font=f)
s.save('renders/cat_poses_sheet.png')
