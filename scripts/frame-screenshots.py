#!/usr/bin/env python3
"""给裸截图套上设备边框，输出成 App Store 要的确切尺寸。

为什么要这一步：设备截图的像素尺寸取决于**拍它的那台设备**，而 App Store 要的是
几个固定档位，两者经常对不上。直接缩放会变形，硬裁会切掉边上的按钮。套边框把这
件事绕开了 —— 截图按原比例缩进屏幕框里，外面补白到目标画布，一个像素都不拉伸。

边框是画出来的，不贴图：画的是圆角矩形加一圈机身，比例照着真机来，但没有任何
Apple 的美术资源 —— 商店截图里出现仿真设备是允许的，照搬素材不是。

用法:
    python3 scripts/frame-screenshots.py <输出目录> <图1> <图2> ...

竖图出 iPhone 框，横图出 iPad 框，按输入图自己的宽高比判断。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

# 目标画布。每张输入图都会按这里的每一档各出一份。
# 竖图走 PORTRAIT，横图走 LANDSCAPE。
# 只有 6.7" 这一档。API 的 screenshotDisplayType 里根本没有 APP_IPHONE_69 ——
# 试着建 6.9" 的 set 会 409，它列出的可选值里最大的 iPhone 就是 APP_IPHONE_67。
PORTRAIT = [
    ("APP_IPHONE_67", 1290, 2796),
]
LANDSCAPE = [
    ("APP_IPAD_PRO_3GEN_129", 2732, 2048),
]


def frame(src, canvas_w, canvas_h, phone):
    """把 src 放进一个画出来的设备框，落在 canvas_w x canvas_h 的白底上。"""
    shot = Image.open(src).convert("RGB")
    sw, sh = shot.size

    # 机身占画布多少。留白不是浪费 —— 商店列表里截图是缩略图，
    # 顶到边的图和相邻那张会糊成一片。
    fill_w, fill_h = (0.86, 0.90) if phone else (0.90, 0.86)

    # 先按「屏幕」算，机身是屏幕加一圈。bezel 用屏幕宽度的比例定，
    # 这样换档位时边框的粗细跟着一起缩，看着是同一台设备。
    bezel_ratio = 0.021 if phone else 0.018

    def device_size(screen_w):
        screen_h = screen_w * sh / sw
        b = screen_w * bezel_ratio
        return screen_w + 2 * b, screen_h + 2 * b

    # 宽高两个方向都不能超，取更紧的那个。
    screen_w = canvas_w * fill_w / (1 + 2 * bezel_ratio)
    if device_size(screen_w)[1] > canvas_h * fill_h:
        # 高度先顶到，反推宽度。
        screen_h = canvas_h * fill_h / (1 + 2 * bezel_ratio * sw / sh)
        screen_w = screen_h * sw / sh

    screen_w = int(round(screen_w))
    screen_h = int(round(screen_w * sh / sw))
    b = int(round(screen_w * bezel_ratio))
    dev_w, dev_h = screen_w + 2 * b, screen_h + 2 * b

    # 圆角。iPhone 的角比 iPad 圆得多，这个差别一眼就能看出来，不能取同一个数。
    dev_r = int(dev_w * (0.115 if phone else 0.042))
    screen_r = max(0, dev_r - b)

    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    x0 = (canvas_w - dev_w) // 2
    y0 = (canvas_h - dev_h) // 2

    # 影子。纯白底上一台纯白设备会糊在一起，得有一点点离地感；
    # 但这是商店截图不是产品渲染图，影子重了喧宾夺主。
    shadow = Image.new("L", (canvas_w, canvas_h), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x0, y0 + int(dev_h * 0.012), x0 + dev_w, y0 + dev_h + int(dev_h * 0.012)],
        radius=dev_r, fill=70,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(dev_w * 0.022)))
    canvas.paste(Image.new("RGB", (canvas_w, canvas_h), (120, 120, 125)), (0, 0), shadow)

    # 机身：外圈浅一档当作金属边，里面是黑色边框。
    body = Image.new("RGB", (dev_w, dev_h), (28, 28, 30))
    bd = ImageDraw.Draw(body)
    bd.rounded_rectangle([0, 0, dev_w - 1, dev_h - 1], radius=dev_r, fill=(206, 206, 211))
    rim = max(2, int(b * 0.30))
    bd.rounded_rectangle(
        [rim, rim, dev_w - 1 - rim, dev_h - 1 - rim],
        radius=max(0, dev_r - rim), fill=(24, 24, 26),
    )

    mask = Image.new("L", (dev_w, dev_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, dev_w - 1, dev_h - 1], radius=dev_r, fill=255)
    canvas.paste(body, (x0, y0), mask)

    # 屏幕内容。按原比例缩放 —— 这一步是整件事的重点，绝不拉伸。
    screen = shot.resize((screen_w, screen_h), Image.LANCZOS)
    smask = Image.new("L", (screen_w, screen_h), 0)
    ImageDraw.Draw(smask).rounded_rectangle(
        [0, 0, screen_w - 1, screen_h - 1], radius=screen_r, fill=255
    )
    canvas.paste(screen, (x0 + b, y0 + b), smask)

    if phone:
        # 灵动岛。截图里那块本来就是白的（截图不含岛本身），补上才是真机的样子。
        # 比例照 iPhone 16 Pro：125x36pt 的胶囊，离屏幕顶 11pt。
        iw = int(screen_w * 0.298)
        ih = int(screen_w * 0.086)
        it = y0 + b + int(screen_w * 0.026)
        ix = x0 + b + (screen_w - iw) // 2
        ImageDraw.Draw(canvas).rounded_rectangle(
            [ix, it, ix + iw, it + ih], radius=ih // 2, fill=(10, 10, 12)
        )

    return canvas


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out_dir, sources = sys.argv[1], sorted(sys.argv[2:])
    os.makedirs(out_dir, exist_ok=True)

    for index, src in enumerate(sources, start=1):
        with Image.open(src) as probe:
            portrait = probe.height >= probe.width
        for display_type, w, h in (PORTRAIT if portrait else LANDSCAPE):
            img = frame(src, w, h, phone=portrait)
            # 文件名带序号，上传脚本按名字排序决定商店页上的先后。
            name = f"{display_type}__{index:02d}.png"
            img.save(os.path.join(out_dir, name), "PNG", optimize=True)
            print(f"{os.path.basename(src):34} -> {name}  {w}x{h}")


if __name__ == "__main__":
    main()
