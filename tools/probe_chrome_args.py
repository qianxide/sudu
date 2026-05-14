"""第二轮:看 HAR 录制 + 完整 --disable-features 列表是否致命。"""
import tempfile, time, traceback
from pathlib import Path
from patchright.sync_api import sync_playwright


def try_launch(label, *, args_extra, ignore_default_args=None, proxy=None,
               record_har=False, init_script=False, full_real_args=False):
    profile = tempfile.mkdtemp(prefix=f"probe-{label}-")
    kwargs = dict(
        user_data_dir=profile,
        channel="chrome",
        headless=False,
        slow_mo=0,
        viewport={"width": 1440, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/Los_Angeles",
        geolocation={"latitude": 37.7749, "longitude": -122.4194},
        permissions=["clipboard-read", "clipboard-write"],
        no_viewport=False,
        args=args_extra,
    )
    if ignore_default_args is not None:
        kwargs["ignore_default_args"] = ignore_default_args
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if record_har:
        har_path = Path("logs/har") / f"probe-{label}-{int(time.time())}.har"
        har_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["record_har_path"] = str(har_path)
        kwargs["record_har_content"] = "embed"
    print(f"\n=== [{label}] HAR={record_har} INIT={init_script} args={args_extra} ===")
    t0 = time.time()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(**kwargs)
            if init_script:
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                    "if(!window.chrome){window.chrome={}}"
                )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://app.flora.ai/sign-in", wait_until="domcontentloaded", timeout=15000)
                print(f"  ✓ [{label}] URL={page.url} title={page.title()[:60]}  ({time.time()-t0:.1f}s)")
                return True
            except Exception as e:
                msg = str(e).split('\n')[0]
                print(f"  ✗ [{label}] goto FAILED: {msg}  ({time.time()-t0:.1f}s)")
                return False
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"  ✗ [{label}] launch failed: {e}")
        traceback.print_exc()
        return False


FULL_FEATURES = ("--disable-features=IsolateOrigins,site-per-process"
                 ",WebAuthentication,WebAuthenticationFidoCableSupport,WebAuthenticationConditionalUI"
                 ",PasswordManager,PasswordManagerOnboarding,AutofillEnableAccountWalletStorage")
FULL_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars", "--disable-extensions",
    "--disable-gpu", "--disable-web-security",
    FULL_FEATURES,
    "--disable-save-password-bubble",
    "--password-store=basic",
]


def main():
    proxy = "http://127.0.0.1:7897"

    cases = [
        # 1) just HAR on top of minimal
        ("har_min", {"args_extra": ["--no-sandbox"], "ignore_default_args": ["--enable-automation"],
                     "proxy": proxy, "record_har": True}),
        # 2) full --disable-features alone (no HAR)
        ("full_features", {"args_extra": ["--no-sandbox", FULL_FEATURES],
                            "ignore_default_args": ["--enable-automation"], "proxy": proxy}),
        # 3) all real flags, NO HAR, NO init
        ("real_no_har", {"args_extra": FULL_ARGS,
                          "ignore_default_args": ["--enable-automation"], "proxy": proxy}),
        # 4) all real flags + HAR
        ("real_with_har", {"args_extra": FULL_ARGS,
                            "ignore_default_args": ["--enable-automation"], "proxy": proxy,
                            "record_har": True}),
        # 5) all real flags + HAR + init_script (= 真正复刻 flora_bot)
        ("real_full", {"args_extra": FULL_ARGS,
                        "ignore_default_args": ["--enable-automation"], "proxy": proxy,
                        "record_har": True, "init_script": True}),
    ]
    results = []
    for label, kw in cases:
        ok = try_launch(label, **kw)
        results.append((label, ok))
        time.sleep(2)

    print("\n=== SUMMARY ===")
    for label, ok in results:
        print(f"  {'✓' if ok else '✗'}  {label}")


if __name__ == "__main__":
    main()
