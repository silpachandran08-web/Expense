"""Builds the Shakebot template images (background + text overlay). Run: python3 make_templates.py"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350  # Instagram portrait post
ORANGE, WHITE, BLACK = (255, 106, 19), (255, 255, 255), (11, 11, 11)
F = '/usr/share/fonts/opentype/inter/'
logo = lambda s: ImageFont.truetype(F + 'InterDisplay-BlackItalic.otf', s)
bold = lambda s: ImageFont.truetype(F + 'Inter-Black.otf', s)
mono = lambda s: ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf', s)


def save(name, draw_fn, bg=BLACK):
    Image.new('RGB', (W, H), bg).save(f'templates/{name}.jpg', quality=92)
    ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(ov))
    ov.save(f'templates/{name}-overlay.png')


def right(d, y, text, font, fill=WHITE, x=W - 70):
    d.text((x, y), text, font=font, fill=fill, anchor='ra')


def protein_decoded(d):
    right(d, 1010, 'SHAKEBOT', logo(64))
    right(d, 1095, 'PROTEIN DECODED', bold(40))
    for i, t in enumerate(['BUILD.RECOVER.FUNCTION', 'STAY STRONG.BUILD.RECOVER', 'FUNCTION.STAY STRONG']):
        right(d, 1150 + i * 32, t, mono(24))


def mono_look(d):
    d.text((W // 2, 1230), 'SHAKEBOT', font=logo(56), fill=(255, 255, 255, 220), anchor='ma')


def grind(d):
    d.text((70, 1110), 'BUILT FOR', font=bold(72), fill=WHITE)
    d.text((70, 1185), 'THE GRIND', font=bold(72), fill=WHITE)
    right(d, 1215, 'SHAKEBOT', logo(44))


def fuel_the_game(d):
    d.text((W // 2, 40), 'FUEL THE GAME', font=bold(132), fill=(240, 236, 228), anchor='ma')
    d.rectangle([150, 205, 930, 285], fill=(236, 232, 224))
    d.text((W // 2, 245), 'Your Protein, Perfected.', font=logo(48), fill=BLACK, anchor='mm')
    right(d, 1255, 'SHAKEBOT', logo(48), fill=ORANGE)


save('protein-decoded', protein_decoded)
save('mono', mono_look)
save('grind', grind)
save('fuel-the-game', fuel_the_game)
