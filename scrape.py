#!/usr/bin/env python3
"""Build Pokémon Champions VGC usage stats from public Pokémon Showdown replays.

Smogon publishes ladder stats once a month, so a new regulation has no usage data
for weeks. This script fills the gap from the public replay archive:

  1. Sample the most recent rated replays of each format to find the rating that
     marks the top 10% of games.
  2. Download every replay at or above that rating (rating-sorted search).
  3. Usage and teammates come from team preview (all six Pokémon, both sides).
     Items, abilities, moves and natures come from open team sheets (`|showteam|`),
     which the Bo3 ladder forces and the Bo1 ladder allows.
  4. Replays never reveal Stat Point spreads. Each observed nature is paired with
     Smogon's most recent spreads for that Pokémon and nature, or with a generic
     max/max estimate when Smogon has none. `info.spreadsEstimated` stays true
     until Smogon's monthly stats cover the current regulation.

Output: stats.json, same shape as https://data.pkmn.cc/stats/*.json plus an `info` block.
Parsed replays are kept in cache/*.jsonl so daily runs only download new games.

Usage: python3 scrape.py [--max-new N]
"""
import concurrent.futures, datetime, json, os, re, sys, time, urllib.error, urllib.request

# ---- per-regulation config: update when the regulation changes ----
REGULATION = "M-C"
FORMATS = ["gen9championsvgc2026regmc", "gen9championsvgc2026regmcbo3"]
# Species first legal in this regulation. Once Smogon's stats contain one, its spreads are current.
NEW_SPECIES = ["Rillaboom", "Salamence", "Golisopod", "Baxcalibur"]
SMOGON_STATS = "https://data.pkmn.cc/stats/gen9championsvgc2026.json"

TOP_FRACTION = 0.10
SAMPLE_PAGES = 40          # recent-replay pages (50 each) sampled for the rating distribution
WINDOW_DAYS = 30           # ignore replays older than this
MIN_SHEETS = 3             # fewer team sheets than this -> fall back to Smogon's set data
UA = "project-red-data/1.0 (+https://github.com/jamesrcode/project-red-data)"
REPLAY = "https://replay.pokemonshowdown.com/"
PS_DATA = "https://play.pokemonshowdown.com/data/"
ROOT = os.path.dirname(os.path.abspath(__file__))


def fetch(url, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # timeouts, resets
            err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"giving up on {url}: {err}")


def to_id(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------- Showdown name tables ----------
def js_names(src):
    """id -> display name from a Showdown client data file (items.js / abilities.js)."""
    return {m.group(1): m.group(2) for m in re.finditer(r'[{,](\w+):\{(?:[^{}]|\{[^{}]*\})*?name:"([^"]+)"', src)}


def load_names():
    pokedex = json.loads(fetch(PS_DATA + "pokedex.json"))
    moves = {k: v["name"] for k, v in json.loads(fetch(PS_DATA + "moves.json")).items()}
    items = js_names(fetch(PS_DATA + "items.js"))
    abilities = js_names(fetch(PS_DATA + "abilities.js"))
    species = {k: v["name"] for k, v in pokedex.items()}
    # mega stone id -> (base species name, mega forme name)
    stones = {}
    for v in pokedex.values():
        if v.get("requiredItem") and v.get("forme", "").startswith("Mega"):
            stones[to_id(v["requiredItem"])] = (v["baseSpecies"], v["name"])
    return species, moves, items, abilities, stones


# ---------- replay listing ----------
def search(fmt, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return json.loads(fetch(f"{REPLAY}search.json?format={fmt}&{q}") or "[]")


def rating_cutoff(fmt, since):
    """Rating at the top-TOP_FRACTION mark, from a sample of the most recent rated replays."""
    ratings, before = [], None
    for _ in range(SAMPLE_PAGES):
        page = search(fmt, **({"before": before} if before else {}))
        if not page:
            break
        ratings += [r["rating"] for r in page if r.get("rating") and r["uploadtime"] >= since]
        before = page[-1]["uploadtime"]
        if len(page) < 51 or before < since:
            break
    if not ratings:
        return None, 0
    ratings.sort(reverse=True)
    return ratings[max(0, int(len(ratings) * TOP_FRACTION) - 1)], len(ratings)


def top_replays(fmt, cutoff, since):
    """(id, rating, uploadtime) of every replay rated >= cutoff. The search caps at 100 pages."""
    out = {}
    for page_no in range(1, 101):
        page = search(fmt, sort="rating", page=page_no)
        if not page:
            break
        for r in page:
            if (r.get("rating") or 0) >= cutoff and r["uploadtime"] >= since:
                out[r["id"]] = (r["id"], r["rating"], r["uploadtime"])
        if (page[-1].get("rating") or 0) < cutoff or len(page) < 51:
            break
    return list(out.values())


# ---------- replay parsing ----------
def parse_log(log):
    teams = {"p1": [], "p2": []}
    sheets = {}
    for line in log.split("\n"):
        if line.startswith("|poke|"):
            _, _, side, details = line.split("|")[:4]
            teams[side].append(details.split(",")[0].replace("-*", ""))
        elif line.startswith("|showteam|"):
            _, _, side, packed = line.split("|", 3)
            mons = []
            for entry in packed.split("]"):
                f = entry.split("|")
                if len(f) < 6:
                    continue
                mons.append({"s": f[1] or f[0], "i": f[2], "a": f[3], "m": [m for m in f[4].split(",") if m], "n": f[5]})
            sheets[side] = mons
        elif line.startswith("|start"):
            break
    return [{"team": teams[s], "sheet": sheets.get(s)} for s in ("p1", "p2") if teams[s]]


def load_cache(fmt):
    path = os.path.join(ROOT, "cache", fmt + ".jsonl")
    if not os.path.exists(path):
        return {}
    return {r["id"]: r for r in map(json.loads, open(path, encoding="utf-8"))}


def save_cache(fmt, cache):
    os.makedirs(os.path.join(ROOT, "cache"), exist_ok=True)
    with open(os.path.join(ROOT, "cache", fmt + ".jsonl"), "w", encoding="utf-8") as f:
        for rid in sorted(cache):
            f.write(json.dumps(cache[rid], separators=(",", ":"), ensure_ascii=False) + "\n")


def download(entry):
    rid, rating, t = entry
    raw = fetch(f"{REPLAY}{rid}.json")
    if raw is None:
        return {"id": rid, "r": rating, "t": t, "sides": []}
    return {"id": rid, "r": rating, "t": t, "sides": parse_log(json.loads(raw).get("log", ""))}


# ---------- spreads ----------
PLUS = {"Lonely": 1, "Adamant": 1, "Naughty": 1, "Brave": 1, "Bold": 2, "Impish": 2, "Lax": 2, "Relaxed": 2,
        "Modest": 3, "Mild": 3, "Rash": 3, "Quiet": 3, "Calm": 4, "Gentle": 4, "Careful": 4, "Sassy": 4,
        "Timid": 5, "Hasty": 5, "Jolly": 5, "Naive": 5}
MINUS = {"Bold": 1, "Modest": 1, "Calm": 1, "Timid": 1, "Lonely": 2, "Mild": 2, "Gentle": 2, "Hasty": 2,
         "Adamant": 3, "Impish": 3, "Careful": 3, "Jolly": 3, "Naughty": 4, "Lax": 4, "Rash": 4, "Naive": 4,
         "Brave": 5, "Relaxed": 5, "Quiet": 5, "Sassy": 5}


def generic_spread(nature, base_stats, physical):
    """Max/max guess (66 Stat Points) for a nature: HP/Atk/Def/SpA/SpD/Spe."""
    sp = [0] * 6
    plus, minus = PLUS.get(nature), MINUS.get(nature)
    if plus is None:  # neutral nature: decide from base stats
        physical = base_stats["atk"] >= base_stats["spa"]
    elif minus == 1:
        physical = False
    elif minus == 3:
        physical = True
    attack = 1 if physical else 3
    if plus == 5:                      # fast attacker
        sp[attack], sp[5], sp[0] = 32, 32, 2
    elif plus in (2, 4):               # defensive
        sp[0], sp[plus], sp[6 - plus] = 32, 32, 2
    else:                              # bulky attacker (incl. Trick Room natures)
        sp[0], sp[attack], sp[4 if attack == 1 else 2] = 32, 32, 2
    return f"{nature}:" + "/".join(map(str, sp))


def estimate_spreads(natures, smogon_spreads, base_stats, physical):
    out = {}
    for nature, w in natures.items():
        known = {k: v for k, v in smogon_spreads.items() if k.startswith(nature + ":")}
        if known:
            top = sorted(known.items(), key=lambda kv: -kv[1])[:4]
            total = sum(v for _, v in top)
            for k, v in top:
                out[k] = out.get(k, 0) + w * v / total
        else:
            k = generic_spread(nature, base_stats, physical)
            out[k] = out.get(k, 0) + w
    return out


# ---------- main ----------
def main():
    max_new = int(sys.argv[sys.argv.index("--max-new") + 1]) if "--max-new" in sys.argv else None
    now = int(time.time())
    since = now - WINDOW_DAYS * 86400
    species_names, move_names, item_names, ability_names, stones = load_names()
    pokedex = json.loads(fetch(PS_DATA + "pokedex.json"))
    moves_data = json.loads(fetch(PS_DATA + "moves.json"))
    smogon = json.loads(fetch(SMOGON_STATS) or "{}").get("pokemon", {})
    smogon_current = any(n in smogon for n in NEW_SPECIES)

    sides, cutoffs, n_replays = [], {}, 0
    for fmt in FORMATS:
        cutoff, sampled = rating_cutoff(fmt, since)
        if cutoff is None:
            print(f"{fmt}: no rated replays", file=sys.stderr)
            continue
        wanted = top_replays(fmt, cutoff, since)
        cache = load_cache(fmt)
        todo = [e for e in wanted if e[0] not in cache][:max_new]
        print(f"{fmt}: top {TOP_FRACTION:.0%} of {sampled} sampled = rating >= {cutoff}; {len(wanted)} replays, {len(todo)} to download", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            for rec in pool.map(download, todo):
                cache[rec["id"]] = rec
        save_cache(fmt, cache)
        cutoffs[fmt] = cutoff
        for rec in cache.values():
            if rec["r"] >= cutoff and rec["t"] >= since and rec["sides"]:
                n_replays += 1
                sides += rec["sides"]

    def species_name(raw):
        """Display name, with cosmetic formes (Vivillon patterns, Alcremie sweets...) folded into the base species."""
        parts = raw.split("-")
        while len(parts) > 1 and to_id("-".join(parts)) not in pokedex:
            parts.pop()
        entry = pokedex.get(to_id("-".join(parts)))
        if entry is None:
            return raw
        base = pokedex.get(to_id(entry.get("baseSpecies", "")), {})
        return base["name"] if entry["name"] in base.get("cosmeticFormes", []) else entry["name"]

    # usage + teammates from team preview
    n_teams = len(sides)
    count, mates = {}, {}
    for side in sides:
        team = [species_name(s) for s in side["team"]]
        for s in team:
            count[s] = count.get(s, 0) + 1
            for o in team:
                if o != s:
                    m = mates.setdefault(s, {})
                    m[o] = m.get(o, 0) + 1

    # sets from team sheets, keyed by the forme the held item implies
    sheets = {}  # preview species -> forme name -> list of sheet mons
    for side in sides:
        for mon in side["sheet"] or []:
            base = species_name(mon["s"])
            stone = stones.get(to_id(mon["i"]))
            forme = stone[1] if stone and stone[0] == pokedex.get(to_id(base), {}).get("baseSpecies", base) else base
            sheets.setdefault(base, {}).setdefault(forme, []).append(mon)

    def dist(mons, key, names):
        out = {}
        for mon in mons:
            vals = mon[key] if isinstance(mon[key], list) else [mon[key]]
            for v in vals:
                if v:
                    name = names.get(to_id(v), v) if names else v
                    out[name] = out.get(name, 0) + 1
        return {k: round(v / len(mons), 4) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}

    pokemon = {}
    for base, c in count.items():
        formes = sheets.get(base) or {base: []}
        n_base_sheets = sum(len(v) for v in formes.values())
        for forme, mons in formes.items():
            share = len(mons) / n_base_sheets if n_base_sheets else 1.0
            usage = round(c / n_teams * share, 5)
            entry = {"usage": {"raw": usage, "real": usage, "weighted": usage}, "count": round(c * share), "sheets": len(mons)}
            fallback = smogon.get(forme, {})
            dex_entry = pokedex.get(to_id(forme), {})
            if len(mons) >= MIN_SHEETS:
                entry["abilities"] = dist(mons, "a", ability_names)
                entry["items"] = dist(mons, "i", item_names)
                entry["moves"] = dist(mons, "m", move_names)
                natures = dist(mons, "n", None)
                physical = sum(1 for mon in mons for m in mon["m"] if moves_data.get(to_id(m), {}).get("category") == "Physical") >= \
                    sum(1 for mon in mons for m in mon["m"] if moves_data.get(to_id(m), {}).get("category") == "Special")
                if smogon_current and fallback.get("spreads"):
                    entry["spreads"] = fallback["spreads"]
                else:
                    entry["spreads"] = estimate_spreads(natures, fallback.get("spreads", {}), dex_entry.get("baseStats", {"atk": 1, "spa": 0}), physical)
                    entry["spreadsEstimated"] = True
            else:
                for k in ("abilities", "items", "moves", "spreads"):
                    entry[k] = fallback.get(k, {})
                entry["setsFromSmogon"] = True
                if not smogon_current:
                    entry["spreadsEstimated"] = True
            for k, n in (("abilities", 4), ("items", 10), ("moves", 14), ("spreads", 8)):
                entry[k] = {a: round(b, 4) for a, b in sorted(entry[k].items(), key=lambda kv: -kv[1])[:n]}
            entry["teammates"] = {k: round(v / c, 4) for k, v in sorted(mates.get(base, {}).items(), key=lambda kv: -kv[1])[:10]}
            pokemon[forme] = entry

    pokemon = dict(sorted(pokemon.items(), key=lambda kv: -kv[1]["usage"]["weighted"]))
    out = {
        "battles": n_replays,
        "info": {
            "generated": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "regulation": REGULATION,
            "source": f"Pokémon Showdown public replays, top {TOP_FRACTION:.0%} by rating, last {WINDOW_DAYS} days",
            "cutoffs": cutoffs,
            "teams": n_teams,
            "sheets": sum(1 for s in sides if s["sheet"]),
            "spreadsEstimated": not smogon_current,
        },
        "pokemon": pokemon,
    }
    if n_teams < 200:
        sys.exit(f"only {n_teams} teams; refusing to overwrite stats.json")
    with open(os.path.join(ROOT, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    print(json.dumps(out["info"], ensure_ascii=False), file=sys.stderr)
    for name, p in list(pokemon.items())[:15]:
        print(f"{p['usage']['weighted']:.1%}  {name}  ({p['sheets']} sheets)", file=sys.stderr)


if __name__ == "__main__":
    main()
