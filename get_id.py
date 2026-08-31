import csv
import json
import random
import ssl
from datetime import datetime, timezone
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

TARGET_URL = "https://connect.vestwell.com/register"
VERIFY_URL = "https://connect.vestwell.com/auth/api/register/verify"
VERIFY_SSL = False
USE_PROXY = True
RESULT_JSON = "get_id_result.json"
RESULT_CSV = "get_id_result.csv"
VERIFY_PAYLOAD = {
    "birthDate": "1968-05-11",
    "lastName": "Rabbass",
    "ssn": "434-27-6540",
}
PROXY_POOL = [
    "6ee246da804e:659bb559d456_country-us_state-florida@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-delaware@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-alabama@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-alaska@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-arizona@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-arkansas@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-california@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-colorado@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-connecticut@proxy.resi.gg:32325",
    "6ee246da804e:659bb559d456_country-us_state-georgia@proxy.resi.gg:32325",
]
HEADLESS = True


def _select_proxy(proxy_list: list[str] = PROXY_POOL) -> dict | None:
    if not USE_PROXY:
        return None
    if not proxy_list:
        return None

    raw_proxy = random.choice(proxy_list)
    if "@" not in raw_proxy:
        return {"raw": raw_proxy, "server": raw_proxy, "http": raw_proxy, "https": raw_proxy}

    auth, host_port = raw_proxy.split("@", 1)
    if ":" not in auth:
        return {"raw": raw_proxy, "server": f"http://{host_port}", "http": f"http://{raw_proxy}", "https": f"http://{raw_proxy}"}

    username, password = auth.split(":", 1)
    return {
        "raw": raw_proxy,
        "server": f"http://{host_port}",
        "username": username,
        "password": password,
        "http": f"http://{raw_proxy}",
        "https": f"http://{raw_proxy}",
    }


def _parse_heap_request(url: str) -> dict | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"heapanalytics.com", "cdn.heapanalytics.com"}:
        return None
    if parsed.path != "/h":
        return None

    params = parse_qs(parsed.query, keep_blank_values=True)
    app_id = params.get("a", [None])[0]
    if not app_id:
        return None

    sp_values = params.get("sp", [])
    sp = {}
    for i in range(0, len(sp_values) - 1, 2):
        key = sp_values[i]
        value = sp_values[i + 1]
        if key == "ts":
            try:
                value = int(value)
            except ValueError:
                pass
        sp[key] = value

    if not sp:
        ts = params.get("ts", [None])[0]
        d = params.get("d", [None])[0]
        h = params.get("h", [None])[0]
        if ts is not None and d is not None and h is not None:
            try:
                ts = int(ts)
            except ValueError:
                pass
            sp = {"ts": ts, "d": d, "h": h}

    return {
        "a": params.get("a", [None])[0],
        "u": params.get("u", [None])[0],
        "v": params.get("v", [None])[0],
        "s": params.get("s", [None])[0],
        "tv": params.get("tv", [None])[0],
        "sp": sp,
    }


def _encode_cookie_json(data: dict) -> str:
    return quote(json.dumps(data, separators=(",", ":"), ensure_ascii=False), safe="-_.!~*'()")


def _build_heap_cookie_payload(found_heap: dict) -> dict:
    if not found_heap:
        return {}

    app_id = found_heap.get("a")
    u = found_heap.get("u")
    v = found_heap.get("v")
    s = found_heap.get("s")

    if not all((app_id, u, v, s)):
        return {}

    sp = found_heap.get("sp", {})
    ts = sp.get("ts")
    d = sp.get("d")
    h = sp.get("h")

    hp2_id_cookie_name = f"_hp2_id.{app_id}"
    hp2_ses_props_cookie_name = f"_hp2_ses_props.{app_id}"

    hp2_id_payload = {
        "userId": str(u),
        "pageviewId": str(v),
        "sessionId": str(s),
        "identity": None,
        "trackerVersion": str(found_heap.get("tv")) if found_heap.get("tv") is not None else None,
    }
    hp2_ses_payload = {
        "ts": ts,
        "d": d,
        "h": h,
    }

    return {
        "hp2_id_cookie_name": hp2_id_cookie_name,
        "hp2_id_cookie_value": _encode_cookie_json(hp2_id_payload),
        "hp2_ses_props_cookie_name": hp2_ses_props_cookie_name,
        "hp2_ses_props_cookie_value": _encode_cookie_json(hp2_ses_payload),
    }


def _find_cookie(cookies: list[dict], name: str) -> dict | None:
    return next((c for c in cookies if c.get("name") == name), None)


def _fetch_public_ip(proxy: dict | None) -> str:
    ip_api_url = "https://api.ipify.org?format=json"
    ssl_context = None if VERIFY_SSL else ssl._create_unverified_context()
    req = urllib.request.Request(ip_api_url, method="GET")
    handlers: list[object] = [urllib.request.HTTPSHandler(context=ssl_context)]
    if proxy:
        handlers.insert(
            0,
            urllib.request.ProxyHandler({"http": proxy["http"], "https": proxy["https"]}),
        )
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=20) as response:
            payload = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(payload).get("ip", "unknown")
            except Exception:
                return payload.strip() or "unknown"
    except Exception as exc:
        return f"failed: {exc}"


def _build_final_cookie_header(
    generated_heap_cookies: dict,
    session_cookie: dict | None,
    waf_cookie: dict | None,
) -> str:
    parts: list[str] = []

    if generated_heap_cookies.get("hp2_id_cookie_name") and generated_heap_cookies.get("hp2_id_cookie_value"):
        parts.append(
            f"{generated_heap_cookies['hp2_id_cookie_name']}={generated_heap_cookies['hp2_id_cookie_value']}"
        )

    if session_cookie:
        parts.append(f"Session={session_cookie['value']}")

    if (
        generated_heap_cookies.get("hp2_ses_props_cookie_name")
        and generated_heap_cookies.get("hp2_ses_props_cookie_value")
    ):
        parts.append(
            f"{generated_heap_cookies['hp2_ses_props_cookie_name']}={generated_heap_cookies['hp2_ses_props_cookie_value']}"
        )

    if waf_cookie:
        parts.append(f"aws-waf-token={waf_cookie['value']}")

    return "; ".join(parts)


def _save_results(result: dict, json_path: str = RESULT_JSON, csv_path: str = RESULT_CSV) -> None:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    heap = result.get("heap", {})
    sp = heap.get("sp", {})

    csv_row = {
        "timestamp": result.get("timestamp"),
        "url": result.get("url"),
        "proxy": result.get("proxy"),
        "a": heap.get("a"),
        "u": heap.get("u"),
        "v": heap.get("v"),
        "s": heap.get("s"),
        "tv": heap.get("tv"),
        "sp_ts": sp.get("ts"),
        "sp_d": sp.get("d"),
        "sp_h": sp.get("h"),
        "final_cookie_header": result.get("final_cookie_header"),
        "Session": result.get("cookies", {}).get("Session"),
        "aws_waf_cookie": result.get("cookies", {}).get("aws-waf-token"),
        "public_ip_via_proxy": result.get("public_ip_via_proxy"),
        "verify_status": result.get("verify", {}).get("status"),
        "verify_body": result.get("verify", {}).get("body"),
    }

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_row.keys()))
        writer.writeheader()
        writer.writerow({k: ("" if v is None else v) for k, v in csv_row.items()})


def _verify_register_post(
    final_cookie_header: str,
    waf_token: str | None,
    user_agent: str | None,
    proxy: dict | None,
) -> dict:
    ssl_context = None if VERIFY_SSL else ssl._create_unverified_context()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://connect.vestwell.com",
        "Referer": "https://connect.vestwell.com/register",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": final_cookie_header,
        "priority": "u=1, i",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    if user_agent:
        headers["User-Agent"] = user_agent
    if waf_token:
        headers["x-aws-waf-token"] = waf_token

    body = json.dumps(VERIFY_PAYLOAD).encode("utf-8")
    req = urllib.request.Request(VERIFY_URL, data=body, method="POST")

    for key, value in headers.items():
        req.add_header(key, value)

    handlers: list[object] = [urllib.request.HTTPSHandler(context=ssl_context)]
    if proxy:
        handlers.insert(
            0,
            urllib.request.ProxyHandler({"http": proxy["http"], "https": proxy["https"]}),
        )
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=30) as response:
            status = response.getcode()
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            return {
                "status": status,
                "ok": response.status == 200,
                "body": raw,
                "content_type": response.headers.get("Content-Type"),
                "set_cookie": response.headers.get("Set-Cookie"),
            }
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read().decode("utf-8", errors="replace")
        return {
            "status": status,
            "ok": False,
            "body": body,
            "content_type": error.headers.get("Content-Type") if error.headers else None,
            "set_cookie": error.headers.get("Set-Cookie") if error.headers else None,
        }
    except Exception as exc:
        return {
            "status": None,
            "ok": False,
            "body": str(exc),
            "content_type": None,
            "set_cookie": None,
        }


def extract_from_single_run(url: str = TARGET_URL) -> dict:
    with Stealth().use_sync(sync_playwright()) as p:
        proxy = _select_proxy()
        browser_proxy = None
        if proxy:
            browser_proxy = {"server": proxy["server"]}
            if proxy.get("username"):
                browser_proxy["username"] = proxy["username"]
            if proxy.get("password"):
                browser_proxy["password"] = proxy["password"]

        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            proxy=browser_proxy,
        )
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=user_agent,
        )
        page = context.new_page()

        found_heap: dict = {}
        analytics_requests: list = []

        def on_request(req):
            parsed_url = req.url
            if any(x in parsed_url for x in ["analytics", "collect", "track", "events"]):
                try:
                    payload = req.post_data_json
                    if payload:
                        analytics_requests.append(payload)
                except Exception:
                    pass

            heap_data = _parse_heap_request(parsed_url)
            if heap_data:
                if not found_heap:
                    found_heap.update(heap_data)
                elif not found_heap.get("sp") and heap_data.get("sp"):
                    found_heap["sp"] = heap_data.get("sp")

        page.on("request", on_request)

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)

        all_cookies = context.cookies()

        session_cookie = _find_cookie(all_cookies, "Session")
        waf_cookie = _find_cookie(all_cookies, "aws-waf-token")

        generated_heap_cookies = _build_heap_cookie_payload(found_heap)
        final_cookie_header = _build_final_cookie_header(generated_heap_cookies, session_cookie, waf_cookie)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "url": url,
            "proxy": proxy["raw"] if proxy else None,
            "public_ip_via_proxy": "not checked",
            "heap": found_heap,
            "analytics_requests": analytics_requests,
            "generated_heap_cookies": generated_heap_cookies,
            "cookies": {
                "Session": session_cookie.get("value") if session_cookie else None,
                "aws-waf-token": waf_cookie.get("value") if waf_cookie else None,
            },
            "final_cookie_header": final_cookie_header,
            "verify": _verify_register_post(
                final_cookie_header=final_cookie_header,
                waf_token=waf_cookie.get("value") if waf_cookie else None,
                user_agent=user_agent,
                proxy=proxy,
            ),
        }

        if proxy:
            print(f"Using proxy: {proxy['raw']}")
            result["public_ip_via_proxy"] = _fetch_public_ip(proxy)
            print(f"IP via proxy: {result['public_ip_via_proxy']}")
        else:
            print("Proxy disabled. Request should go directly from current environment IP.")

        print("=== Extracted values ===")
        if found_heap:
            print(f"a  = {found_heap.get('a')}")
            print(f"u  = {found_heap.get('u')}")
            print(f"v  = {found_heap.get('v')}")
            print(f"s  = {found_heap.get('s')}")
            print(f"tv = {found_heap.get('tv')}")
            print()
            print("sp:")
            print(f"ts = {found_heap.get('sp', {}).get('ts')}")
            print(f"d  = {found_heap.get('sp', {}).get('d')}")
            print(f"h  = {found_heap.get('sp', {}).get('h')}")
        else:
            print("a/u/v/s/tv/sp: not found in captured network requests")

        print("\n=== Heap-generated cookies ===")
        if generated_heap_cookies.get("hp2_id_cookie_name"):
            print(f"{generated_heap_cookies['hp2_id_cookie_name']}={generated_heap_cookies['hp2_id_cookie_value']}")
        if generated_heap_cookies.get("hp2_ses_props_cookie_name"):
            print(
                f"{generated_heap_cookies['hp2_ses_props_cookie_name']}={generated_heap_cookies['hp2_ses_props_cookie_value']}"
            )

        print("\nCookies from browser:")
        print(f"Session = {session_cookie['value'] if session_cookie else '<not found>'}")
        print(f"aws-waf-token = {waf_cookie['value'] if waf_cookie else '<not found>'}")

        print("\n=== Final Cookie Header ===")
        print(final_cookie_header)

        if analytics_requests:
            print("\n--- Analytics payloads captured (json) ---")
            for i, payload in enumerate(analytics_requests[:5], 1):
                try:
                    print(f"[{i}] {json.dumps(payload, ensure_ascii=False)}")
                except Exception:
                    print(f"[{i}] {payload}")

        print("\n=== Register Verify Response ===")
        verify = result["verify"]
        print(f"Status: {verify.get('status')}")
        print(f"Content-Type: {verify.get('content_type')}")
        if verify.get("body"):
            print(f"Body: {verify.get('body')}")

        browser.close()

        _save_results(result)
        print(f"\nSaved JSON -> {RESULT_JSON}")
        print(f"Saved CSV  -> {RESULT_CSV}")

        return result


if __name__ == "__main__":
    extract_from_single_run(TARGET_URL)
