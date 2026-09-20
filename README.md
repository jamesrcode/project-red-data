# project-red-data

Daily usage stats for **Pokémon Champions VGC**, built from the top 10% (by rating) of public
[Pokémon Showdown](https://replay.pokemonshowdown.com/) ladder replays. Smogon only publishes
ladder stats once a month, so this fills the gap at the start of each regulation and keeps the
numbers current in between.

`stats.json` is regenerated every day by GitHub Actions:

```
https://raw.githubusercontent.com/jamesrcode/project-red-data/main/stats.json
```

## What's in it

Same shape as the [pkmn stats mirror](https://data.pkmn.cc/) (`pokemon.<name>.usage / abilities / items / moves / spreads / teammates`), plus an `info` block (regulation, rating cutoffs, sample sizes).

| Field | Where it comes from |
|---|---|
| usage, teammates | Team preview of every sampled replay, both sides |
| items, abilities, moves | Open team sheets (`showteam`): forced on the Bo3 ladder, optional on Bo1. Mega formes are split out by held Mega Stone. |
| spreads | **Estimated** while `spreadsEstimated` is true. Replays show natures but not Stat Points, so each observed nature is paired with Smogon's latest spreads for that Pokémon and nature, or a generic max/max guess. Once Smogon's monthly stats cover the current regulation their real spreads are used. |

A Pokémon with fewer than 3 team sheets falls back to Smogon's last monthly set data (`setsFromSmogon`).

## Running

```sh
python3 scrape.py            # standard library only
```

Parsed replays are cached in `cache/` so each run only downloads new games. When the regulation
changes, update the config block at the top of `scrape.py`.

Replay data © its players / Pokémon Showdown; fallback set data from Smogon usage stats via pkmn.cc.
