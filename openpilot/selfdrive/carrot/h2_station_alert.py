#!/usr/bin/env python3
"""Nexo hydrogen station alert: once per drive, when the tank is low, list the
nearest open hydrogen stations on the cluster HUD and send them to Telegram.

Read-only with respect to the car: it only reads carState.fuelGauge and GPS.
Idle unless /data/h2_alert.env holds DATA_GO_KR_KEY (TELEGRAM_* optional).
Station positions/prices come from the KPETRO station list CSV (cached, refreshed
weekly); open/closed state and queue length from the KPETRO realtime API.
The HUD reads /dev/shm/h2_station.json through the Jetlink display snapshot.
"""
import csv
import io
import json
import math
import os
import time
import urllib.parse
import urllib.request

from openpilot.cereal import messaging
from openpilot.common.swaglog import cloudlog

CONFIG_PATH = "/data/h2_alert.env"
CACHE_DIR = "/data/h2_alert"
STATIONS_CSV = os.path.join(CACHE_DIR, "stations.csv")
LAST_POS_PATH = os.path.join(CACHE_DIR, "last_pos.json")
HUD_PATH = "/dev/shm/h2_station.json"
STATIONS_URL = "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000007629002&fileDetailSn=1&insertDataPrcus=N"
LIVE_URL = "https://apis.data.go.kr/B552532/h2nbiz_3/currentInfo?serviceKey={}&numOfRows=1000&pageNo=1"
NAV_URL = "https://sangok0207-del.github.io/h2-go/?lat={lat}&lng={lng}&name={name}&app="
STATIONS_MAX_AGE_S = 7 * 86400
HUD_SHOW_S = 60          # panel stays on the cluster this long after ignition
WAIT_FOR_DATA_S = 180    # give the fuel gauge this long to appear
WAIT_FOR_GPS_S = 20      # then fall back to the last known position (parking garages block GPS)
DEFAULT_THRESHOLD = 100  # owner asked for the panel and Telegram on every ignition
INFO_PATH = "/dev/shm/h2_info.json"
TANK_KG = 6.33           # Nexo usable hydrogen at 100 %
MIN_USED_PCT = 1.0       # hide economy until at least 1 % (two 0.5 % steps) is used
MAX_ATTEMPTS = 5
RETRY_S = 30


def load_config() -> dict[str, str]:
  try:
    with open(CONFIG_PATH, encoding="utf-8") as f:
      return {k.strip(): v.strip().strip('"') for k, v in (l.split("=", 1) for l in f if "=" in l and not l.startswith("#"))}
  except OSError:
    return {}


def http_get(url: str, timeout: float = 20) -> bytes:
  req = urllib.request.Request(url, headers={"User-Agent": "carrot-h2-alert"})
  with urllib.request.urlopen(req, timeout=timeout) as r:
    return r.read()


def load_stations() -> dict[str, dict]:
  os.makedirs(CACHE_DIR, exist_ok=True)
  stale = not os.path.isfile(STATIONS_CSV) or time.time() - os.path.getmtime(STATIONS_CSV) > STATIONS_MAX_AGE_S
  if stale:
    try:
      data = http_get(STATIONS_URL, 60)
      if len(data) > 10000:
        with open(STATIONS_CSV + ".tmp", "wb") as f:
          f.write(data)
        os.replace(STATIONS_CSV + ".tmp", STATIONS_CSV)
    except Exception:
      cloudlog.exception("h2_alert: station list download failed")
  out = {}
  with io.open(STATIONS_CSV, encoding="utf-8-sig") as f:
    for d in csv.DictReader(f):
      try:
        d["lat"], d["lon"] = float(d["위도"]), float(d["경도"])
      except (KeyError, ValueError):
        continue
      out[d["충전소 관리번호"]] = d
  return out


def km(lat1, lon1, lat2, lon2) -> float:
  p = math.pi / 180
  a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
  return 12742 * math.asin(math.sqrt(a))


def nearest_open(key: str, lat: float, lon: float, n: int = 3) -> list[dict]:
  stations = load_stations()
  items = json.loads(http_get(LIVE_URL.format(key)))["body"]["items"]["item"]
  live = {i["chrstn_mno"]: i for i in items}
  res = []
  for mno, s in stations.items():
    lv = live.get(mno)
    if not lv or lv.get("oper_sttus_nm") != "운영중" or "승용" not in s.get("충전가능차량", "승용"):
      continue
    res.append({
      "name": s["충전소명"], "km": round(km(lat, lon, s["lat"], s["lon"]), 1), "lat": s["lat"], "lon": s["lon"],
      "wait": int(lv.get("wait_vhcle_alge") or 0), "congestion": lv.get("cnf_sttus_nm") or "",
      "price": int(s["판매가격"]) if (s.get("판매가격") or "").isdigit() else None,
    })
  res.sort(key=lambda r: r["km"])
  return res[:n]


def write_hud(info: dict | None) -> None:
  if info is None:
    try:
      os.unlink(HUD_PATH)
    except FileNotFoundError:
      pass
    return
  with open(HUD_PATH + ".tmp", "w", encoding="utf-8") as f:
    json.dump(info, f, ensure_ascii=False)
  os.replace(HUD_PATH + ".tmp", HUD_PATH)


def send_telegram(cfg: dict[str, str], h2: int, stations: list[dict]) -> None:
  tok, chat = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
  if not tok or not chat:
    return
  lines = [f"⛽ 수소 {h2}% — 가까운 운영 중 충전소"]
  buttons = []
  for i, s in enumerate(stations, 1):
    price = f"{s['price']:,}원/kg" if s["price"] else "가격 정보 없음"
    lines.append(f"\n{i}. {s['name']}\n   {s['km']}km · 대기 {s['wait']}대 · {s['congestion']} · {price}")
    name = urllib.parse.quote(s["name"])
    go = NAV_URL.format(lat=s["lat"], lng=s["lon"], name=name)
    buttons.append([{"text": f"{i} 네이버", "url": go + "naver"}, {"text": f"{i} 티맵", "url": go + "tmap"},
                    {"text": f"{i} 카카오", "url": f"https://map.kakao.com/link/to/{name},{s['lat']},{s['lon']}"}])
  if not stations:
    lines.append("\n주변에 운영 중인 충전소를 찾지 못했습니다.")
  body = json.dumps({"chat_id": chat, "text": "\n".join(lines), "reply_markup": {"inline_keyboard": buttons}}).encode()
  req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", body, {"Content-Type": "application/json"})
  urllib.request.urlopen(req, timeout=15).read()


def save_pos(lat: float, lon: float) -> None:
  try:
    with open(LAST_POS_PATH, "w") as f:
      json.dump({"lat": lat, "lon": lon}, f)
  except OSError:
    pass


def load_pos() -> tuple[float, float] | None:
  try:
    with open(LAST_POS_PATH) as f:
      d = json.load(f)
    return float(d["lat"]), float(d["lon"])
  except Exception:
    return None


class TripEconomy:
  """km/kg since ignition: integrated vEgo over hydrogen used (0x511 has 0.5 % steps)."""
  def __init__(self):
    self.reset()

  def reset(self):
    self.start_pct = None
    self.km = 0.0

  def update(self, v_ego: float, dt: float, fuel: float) -> float | None:
    if fuel <= 0:
      return None
    pct = fuel * 100
    if self.start_pct is None or pct > self.start_pct + 1.0:  # first reading or refuelled
      self.start_pct, self.km = pct, 0.0
    self.km += max(0.0, v_ego) * dt / 1000.0
    used_kg = (self.start_pct - pct) / 100 * TANK_KG
    if self.start_pct - pct < MIN_USED_PCT or used_kg <= 0:
      return None
    return self.km / used_kg


class RangeReader:
  """Cluster frames on bus 0, sampled from a conflated can socket:
  CLU13 (0x50C) CF_Clu_DTE bit 24 len 10 = range km;
  0x52C byte1 >> 4 = regen paddle level 0-3, byte2 = drive mode (0 NORMAL, 64 ECO, 96 ECO+),
  both found 2026-10-23 by stepping the paddles and mode switch while parked."""
  MODES = {0: "NORMAL", 64: "ECO", 96: "ECO+"}

  def __init__(self):
    self.sock = messaging.sub_sock("can", conflate=True)

  def read(self) -> dict:
    out = {}
    for _ in range(60):
      msg = messaging.recv_one_or_none(self.sock)
      if msg is not None:
        for c in msg.can:
          if c.src != 0:
            continue
          if c.address == 0x50C and len(c.dat) >= 5:
            out["range_km"] = c.dat[3] | ((c.dat[4] & 0x3) << 8)
          elif c.address == 0x52C and len(c.dat) >= 3:
            out["regen"] = (c.dat[1] >> 4) & 0x7
            out["mode"] = self.MODES.get(c.dat[2] & 0x60)
        if "range_km" in out and "regen" in out:
          break
      time.sleep(0.01)
    return out


def write_info(info: dict) -> None:
  with open(INFO_PATH + ".tmp", "w", encoding="utf-8") as f:
    json.dump(info, f)
  os.replace(INFO_PATH + ".tmp", INFO_PATH)


def main() -> None:
  try:
    os.nice(19)
  except OSError:
    pass
  sm = messaging.SubMaster(["deviceState", "carState", "gpsLocation"])
  economy, range_reader = TripEconomy(), RangeReader()
  econ = None
  cluster: dict = {}
  last_t = info_at = 0.0
  started_prev = False
  started_t = 0.0
  done = True
  hide_at = 0.0
  attempts, retry_at, saved_at = 0, 0.0, 0.0
  write_hud(None)
  while True:
    time.sleep(0.2)
    sm.update(0)
    now = time.monotonic()
    started = bool(sm["deviceState"].started)
    if started and not started_prev:
      started_t, done, attempts, retry_at = now, False, 0, 0.0
      economy.reset()
    started_prev = started

    if started:
      cs = sm["carState"]
      econ = economy.update(cs.vEgo, min(now - last_t, 1.0) if last_t else 0.0, cs.fuelGauge)
      if now - info_at >= 2.0:
        cluster.update(range_reader.read())
        write_info({**cluster, "econ": round(econ, 1) if econ else None})
        info_at = now
    last_t = now

    gps = sm["gpsLocation"]
    pos = (gps.latitude, gps.longitude) if sm.recv_frame["gpsLocation"] > 0 and gps.hasFix and abs(gps.latitude) > 1 else None
    if pos is not None and now - saved_at > 60:
      save_pos(*pos)
      saved_at = now

    if hide_at and now >= hide_at:
      write_hud(None)
      hide_at = 0.0
    if not started:
      if hide_at:
        write_hud(None)
        hide_at = 0.0
      continue
    if done or now < retry_at:
      continue

    cfg = load_config()
    key = cfg.get("DATA_GO_KR_KEY")
    fuel = sm["carState"].fuelGauge
    waited = now - started_t
    if not key or (fuel <= 0 and waited < WAIT_FOR_DATA_S):
      done = not key
      continue
    if pos is None and waited < WAIT_FOR_GPS_S:
      continue
    done = True
    h2 = int(round(fuel * 100))
    threshold = int(cfg.get("THRESHOLD") or DEFAULT_THRESHOLD)
    if fuel <= 0 or h2 > threshold:
      continue
    pos = pos or load_pos()
    if pos is None:
      continue
    save_pos(*pos)
    try:
      stations = nearest_open(key, *pos)
      write_hud({"h2": h2, "stations": stations})
      hide_at = now + HUD_SHOW_S
      send_telegram(cfg, h2, stations)
      cloudlog.info(f"h2_alert: h2={h2}% stations={[s['name'] for s in stations]}")
    except Exception:
      # LTE often comes up after ignition; retry a few times before giving up.
      cloudlog.exception("h2_alert: lookup failed")
      attempts += 1
      if attempts < MAX_ATTEMPTS:
        done, retry_at = False, now + RETRY_S


if __name__ == "__main__":
  main()
