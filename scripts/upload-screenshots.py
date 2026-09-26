#!/usr/bin/env python3
"""把截图传到 App Store Connect 上的 0.1.0 版本。

网页端传截图很快，但每次改版都要重来一遍，而且拖错一个尺寸格子不会当场报错。
这个脚本按**图片自己的像素尺寸**决定它属于哪个 display type，传错尺寸会在上传
之前就被挡下来。

用法:
    python3 scripts/upload-screenshots.py <目录或若干图片文件> [--replace]

同一尺寸的多张图按文件名排序决定在商店页上的先后，所以文件名带上序号：
    01-writing.png  02-answer.png  03-offline.png

PNG 和 JPEG 都收，但尺寸必须**正好**等于 App Store 认的那几个 —— 缩过的图
不能靠放大补回来，糊在商店页上比没有更难看。

--replace 会先清掉该尺寸下已有的截图。不加的话是追加。

依赖 /root/asc_api.py 里的 JWT 签发（pyjwt + cryptography）。
"""
import hashlib
import json
import os
import struct
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/root")
from asc_api import req, token  # noqa: E402

APP_ID = "6815647930"
VERSION = "0.1.0"

# 像素尺寸 → App Store Connect 的 display type。
#
# 竖屏横屏都列出来：同一块屏转 90 度是两个合法尺寸，但属于同一个 display type。
# 表里没有的尺寸一律拒掉 —— 传上去 Apple 也会退，不如在本地就说清楚。
SIZES = {
    # API 的 screenshotDisplayType 枚举里没有 6.9" 那一档，iPhone 最大就到 6.7"。
    (1290, 2796): "APP_IPHONE_67",
    (2796, 1290): "APP_IPHONE_67",
    (1284, 2778): "APP_IPHONE_67",
    (2778, 1284): "APP_IPHONE_67",
    (1242, 2688): "APP_IPHONE_65",
    (2688, 1242): "APP_IPHONE_65",
    (2064, 2752): "APP_IPAD_PRO_3GEN_129",  # iPad Pro 13" (M4)
    (2752, 2064): "APP_IPAD_PRO_3GEN_129",
    (2048, 2732): "APP_IPAD_PRO_3GEN_129",  # 12.9"
    (2732, 2048): "APP_IPAD_PRO_3GEN_129",
}


def image_size(path):
    """读图片头拿尺寸。不引 Pillow —— 这台机器上不一定装着，而这点活不值得加依赖。"""
    data = open(path, "rb").read()

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])

    if data[:2] == b"\xff\xd8":
        # JPEG 得一段段跳过去找 SOF，尺寸不在固定偏移上。
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            if marker == 0xD8 or 0xD0 <= marker <= 0xD9:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        raise SystemExit(f"{path} 是 JPEG，但读不出尺寸。")

    raise SystemExit(f"{path} 既不是 PNG 也不是 JPEG。App Store 只收这两种。")


def api(method, path, body=None, expect=(200, 201, 204)):
    status, text = req(method, path, body)
    if status not in expect:
        raise SystemExit(f"{method} {path} -> HTTP {status}\n{text}")
    return json.loads(text) if text.strip() else None


def localization_id():
    versions = api("GET", f"/v1/apps/{APP_ID}/appStoreVersions?filter[versionString]={VERSION}")
    if not versions["data"]:
        raise SystemExit(f"App Store Connect 上没有 {VERSION} 这个版本。")
    version_id = versions["data"][0]["id"]
    locs = api("GET", f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations")
    for loc in locs["data"]:
        if loc["attributes"]["locale"] == "en-US":
            return loc["id"]
    raise SystemExit("没找到 en-US 的本地化条目。")


def screenshot_set(loc_id, display_type, replace):
    """拿到这个尺寸的 set，没有就建一个。"""
    sets = api("GET", f"/v1/appStoreVersionLocalizations/{loc_id}/appScreenshotSets")
    for s in sets["data"]:
        if s["attributes"]["screenshotDisplayType"] == display_type:
            if replace:
                shots = api("GET", f"/v1/appScreenshotSets/{s['id']}/appScreenshots")
                for shot in shots["data"]:
                    api("DELETE", f"/v1/appScreenshots/{shot['id']}", expect=(204,))
                    print(f"    删掉旧图 {shot['id']}")
            return s["id"]
    created = api("POST", "/v1/appScreenshotSets", {
        "data": {
            "type": "appScreenshotSets",
            "attributes": {"screenshotDisplayType": display_type},
            "relationships": {
                "appStoreVersionLocalization": {
                    "data": {"type": "appStoreVersionLocalizations", "id": loc_id}
                }
            },
        }
    })
    print(f"    新建 set {display_type}")
    return created["data"]["id"]


def upload(path, set_id):
    """三步走：预约 → 按 Apple 给的分片切开上传 → 提交校验和。

    分片是 Apple 那边定的，不是我们挑的：它在 uploadOperations 里给出每一段的
    offset/length 和要带的 header，照着发就行。少发一段或者顺序乱了，最后
    commit 那一步会以校验和不符失败。
    """
    data = open(path, "rb").read()
    reserved = api("POST", "/v1/appScreenshots", {
        "data": {
            "type": "appScreenshots",
            "attributes": {"fileSize": len(data), "fileName": os.path.basename(path)},
            "relationships": {
                "appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}
            },
        }
    })
    shot_id = reserved["data"]["id"]

    for op in reserved["data"]["attributes"]["uploadOperations"]:
        chunk = data[op["offset"]:op["offset"] + op["length"]]
        request = urllib.request.Request(op["url"], data=chunk, method=op["method"])
        for header in op["requestHeaders"]:
            request.add_header(header["name"], header["value"])
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"上传分片失败 HTTP {e.code}: {e.read().decode()[:400]}")

    api("PATCH", f"/v1/appScreenshots/{shot_id}", {
        "data": {
            "type": "appScreenshots",
            "id": shot_id,
            "attributes": {
                "uploaded": True,
                "sourceFileChecksum": hashlib.md5(data).hexdigest(),
            },
        }
    })
    return shot_id


def main():
    args = [a for a in sys.argv[1:] if a != "--replace"]
    replace = "--replace" in sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)

    paths = []
    for arg in args:
        if os.path.isdir(arg):
            paths += [
                os.path.join(arg, f) for f in os.listdir(arg)
                if f.lower().endswith((".png", ".jpg", ".jpeg"))
            ]
        else:
            paths.append(arg)
    # 文件名排序 = 商店页上的排列顺序，所以名字前面要带序号。
    paths.sort()
    if not paths:
        raise SystemExit("没找到图片。")

    # 先把所有图验一遍再传。传到一半才发现有张尺寸不对，商店页上会留下半套图。
    by_type = {}
    for path in paths:
        size = image_size(path)
        display_type = SIZES.get(size)
        if display_type is None:
            raise SystemExit(
                f"{path} 是 {size[0]}x{size[1]}，不是 App Store 认的截图尺寸。\n"
                f"认这些：{sorted(set(SIZES))}"
            )
        by_type.setdefault(display_type, []).append(path)
        print(f"{os.path.basename(path):40} {size[0]}x{size[1]}  -> {display_type}")

    token()  # 先签一次，密钥不对的话在这儿就炸，别等传到一半
    loc_id = localization_id()
    print(f"\nen-US 本地化 {loc_id}，版本 {VERSION}")

    for display_type, group in by_type.items():
        print(f"\n{display_type}  {len(group)} 张")
        set_id = screenshot_set(loc_id, display_type, replace)
        for path in group:
            shot_id = upload(path, set_id)
            print(f"    传完 {os.path.basename(path)} -> {shot_id}")

    print("\n全部传完。Apple 那边要过几分钟才处理完，处理完了截图才算数。")


if __name__ == "__main__":
    main()
