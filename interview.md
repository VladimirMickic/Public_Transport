# EMTA Reliability Tracker — Stakeholder Briefing & Mock Interview

> Prep doc for presenting the project to the **Erie Metropolitan Transit Authority**.
> Treat the EMTA voice as a skeptical, experienced operations director who has seen
> three vendors come through with dashboards that didn't survive contact with reality.

---

## Part 1 — Your opening (90 seconds, memorize the shape)

> "I rode EMTA buses every day. One Friday I missed a connection because route 5 was eleven minutes late, and I had no way to know if that was unusual for that route, that hour, or that day. Google gave me a live ETA but no history, and there was no public answer to *'is this route reliable on Friday afternoons?'* anywhere.
>
> So I built one. Every five minutes, an automated pipeline pulls live vehicle positions from your Avail InfoPoint API, scores on-time performance using the same OTP formula defined in TCRP Report 88 — the standard reference for transit performance measurement — and produces per-route, per-hour, per-day reliability numbers. The formula is the same one WMATA, TriMet, and AC Transit use; the on-time window I picked (−1 minute to +5 minutes) is the most common modern threshold, though peer agencies pick slightly different windows. A dashboard renders the numbers for the public, and a Claude AI agent writes a plain-English daily and weekly digest of where the system stood out, good or bad.
>
> The whole thing runs on free tiers. No vendor lock-in, no cost to EMTA, source code is open. I'm not here to sell anything — I'm here to show you a rider's view of your system and to ask whether anything I've built is useful to you, and whether anything I've built is wrong."

**Why that opening works:**
- Personal story → not a consultant pitch
- TCRP 88 → signals you did your homework, gives them something they recognize
- "Free tiers" → removes the "what's this going to cost us" defense before they raise it
- "Anything I've built is wrong" → invites correction, which is what wins technical respect

---

## Part 2 — The 5-slide narrative (if you have a screen)

| # | Slide | One-line takeaway |
|---|---|---|
| 1 | The rider problem | "I had no way to know if my route was usually late or just late today." |
| 2 | The data flow (Bronze→Silver→Gold) | "Every ping is preserved raw, then cleaned, then aggregated. We can never lose source data." |
| 3 | The reliability formula (TCRP 88) | "OTP% = on-time pings / total. Same formula peer agencies report. Benchmarkable." |
| 4 | The dashboard (5 tabs) | "Overview, Route Detail, Live Map, Daily Digest, Weekly Digest." |
| 5 | What this is and isn't | "A reliability tracker, not a navigator. Tells you which bus to trust, not when it's coming." |

---

## Part 3 — Project facts you must have at your fingertips

**Numbers to memorize:**
- Polling interval: **every 5 minutes** during the 5 AM–11 PM service window
- Pings per day: ~12,000–15,000 depending on fleet size
- On-time window: **−1 minute early to +5 minutes late** (TCRP standard)
- Delay buckets: `early` (<−1), `on_time` (−1 to +5), `late` (5–15), `very_late` (>15)
- Routes excluded from analytics: **98 (AM Tripper), 99 (PM Tripper), 999 (Deadhead)** — non-passenger
- Tech stack cost: **$0/month** (Supabase free, GitHub Actions free, Oracle Cloud free VM, Streamlit Cloud free)
- AI cost: pennies per month thanks to cache-first on-demand pattern
- Two redundant cron runners: GitHub Actions (every 5 min, best-effort) + Oracle VM (millisecond-accurate). Idempotent writes mean no duplicates.

**Architecture in one breath:**
- Bronze (raw API dumps with full `raw_json` backup) → Silver (ET-timezone, parked-bus filter, delay bucket assigned) → Gold (aggregated by route × hour × day-of-week, lifetime UPSERT)
- Postgres on Supabase, no ORM, plain `psycopg2`
- Streamlit dashboard with `@st.cache_data` (300s default, 60s for live views)

---

## Part 4 — Mock interview: questions EMTA will ask you

The questions are ordered by likelihood. Read each, try to answer in your own words *before* reading my answer.

---

### Q1. "Where is your data coming from? And do you have permission to use it?"

**Why they ask:** Their first job is to protect the agency. If you're scraping a private endpoint, this conversation ends here.

**Your answer:**
> "The Avail InfoPoint API. It's the same endpoint your public-facing real-time bus tracker on emtaerie.com calls — I'm not bypassing any authentication, not scraping HTML, and I'm not hammering it. One request every five minutes, which is well below the rate the public-facing tracker generates from a single user session. If you'd prefer I use a different endpoint, or if there's an official data-sharing arrangement you'd want in place, I'm happy to switch."

**Follow-up they may ask:** "Are you republishing raw GPS positions?"
> "The Live Map tab does show current vehicle positions for the last 15 minutes — same as your own public tracker does. Nothing historical at the individual-vehicle level is published. Aggregates only."

---

### Q2. "TCRP Report 88 is from 2003. Is your formula actually current?"

**Why they ask:** Testing whether you parroted a citation or actually understand it.

**Your answer:**
> "TCRP 88 is the foundational reference, and the formula — OTP% = on-time pings divided by total pings — hasn't changed since because it doesn't need to. What *has* evolved is the on-time threshold. The 2003 default was 0 to +5 minutes; modern practice, including FTA's National Transit Database guidance and most published service standards I looked at — TriMet, AC Transit, WMATA — uses **−1 to +5**, because a bus that's 2 minutes early strands every rider who showed up at the scheduled time. I use the modern threshold and it's a single config value if EMTA wants a different one."

**Follow-up:** "Why not headway-based reliability for the high-frequency routes?"
> "Fair challenge — for routes that come every 10 minutes or less, riders don't read schedules, they just show up. Headway adherence is the right metric there. I don't currently distinguish; that's a real limitation. If you tell me which routes EMTA considers high-frequency, I can add a headway calculation alongside OTP."

---

### Q3. "Our own internal numbers show route X at 82%. Yours shows 76%. Why should we trust yours?"

**Why they ask:** This *will* happen. Their numbers will disagree with yours and they'll want to know which to trust.

**Your answer:**
> "Don't trust mine over yours — investigate the gap. There are four likely sources of divergence, and they're all worth checking:
>
> 1. **Window definition.** What counts as on-time for EMTA internally? If you use 0 to +5 and I use −1 to +5, my number will be lower because I'm penalizing early departures.
> 2. **Sample.** I exclude routes 98, 99, and 999, and I exclude pings where speed < 2 mph, which drops parked buses and layovers. If your internal number includes layover time, you'll get a different denominator.
> 3. **Timezone.** I had a bug for a week where pings from 8–11 PM ET were getting stamped with the next day's UTC date. If your dates and mine don't align, the buckets won't either.
> 4. **Polling cadence.** I sample every 5 minutes. If your AVL system records every 30 seconds, you have 10× the resolution and small-amplitude swings show up in your data that mine smooths out.
>
> The honest answer is that any single number is a function of how you defined it. The point of using TCRP is that we're at least defining it the same way as the rest of the industry."

**This is the answer that wins them.** It says "I'm not here to embarrass you, I'm here to compare apples to apples."

---

### Q4. "What happens when our API goes down?"

**Your answer:**
> "Three things. First, the pipeline keeps running — `fetch_realtime.py` catches request exceptions, logs them, and writes nothing to Bronze for that interval, rather than crashing. Second, downstream Silver and Gold use idempotent writes — Silver deletes the date window before reinserting, Gold uses ON CONFLICT DO UPDATE — so if the API comes back and a backfill run captures the same window, no duplicates. Third, the dashboard's KPIs are computed from whatever Silver has; missing pings just produce smaller denominators for that hour. There's no silent corruption from an outage; there's just less data for that window."

**Follow-up:** "What if you get bad data — a bus stuck reporting 0 mph for an hour?"
> "That's why the speed filter exists. Pings with speed < 2 mph are excluded from Silver. It's a blunt instrument — a bus stopped at a long red light is also < 2 mph for 90 seconds — but on aggregate it's the right call because layovers and breakdowns generate dramatically more low-speed pings than stoplights do."

---

### Q5. "How do we know your AI isn't making things up? Hallucination is a real concern."

**Why they ask:** This is the question that separates "interesting toy" from "could we use it." LLMs writing about agency performance is a reputational risk.

**Your answer:**
> "Two safeguards, both deliberate, both because Claude tried to hallucinate and I had to stop it.
>
> **First, the KPI snapshot freeze.** Every digest stamps a JSONB blob with the exact numbers Claude was shown at generation time — total pings, OTP%, the worst routes table, the hourly arc. The dashboard renders the narrative, the KPI strip, the chart, and the worst-routes table all from that same blob. So the narrative cannot disagree with the numbers next to it, because they come from the same source.
>
> **Second, a scrub-and-inject step.** I instructed Claude in the prompt not to cite system-wide percentages, because it kept inventing them — saw 72.2% in the prompt, wrote 73.8% in the output. It ignored the instruction. So after Claude returns, my code detects any paragraph that smells like a stats summary, drops it, and inserts a deterministic sentence built from the snapshot. The narrative is mathematically prevented from contradicting the KPI strip.
>
> If a digest still comes out wrong — a misread, a weird tone — there's a 'Report a bad digest' button on every past-day digest that re-runs generation. That click is logged with a `generation_count` so I can see how often regeneration is needed."

**Follow-up:** "Could the AI defame a route or a driver?"
> "Drivers are never named — vehicle IDs are stripped from the AI prompt. The prompt explicitly forbids naming routes 98, 99, 999, and forbids speculation about causes (no 'driver may have been...'). And again — the dashboard is read-only; nothing the AI writes is autoposted to social media. EMTA owns the story, the digest is just a draft."

---

### Q6. "What's it cost to run this? Who pays when it grows?"

**Your answer:**
> "Today, zero dollars per month. Supabase free tier (500 MB), GitHub Actions free tier (2,000 minutes/month, the pipeline uses about 400), Oracle Cloud Always Free VM, Streamlit Community Cloud, and Anthropic API costs that round to less than a dollar a month because of the cache-first AI pattern.
>
> Where it would break: if Bronze grows past 500 MB, Supabase would push me to a paid tier. I built `maintenance/prune_old_data.py` to keep Bronze under that ceiling — it prunes by storage usage, not age, so it keeps the most recent data possible for the free tier. If EMTA wanted full historical retention, that's a real cost, but it's $25/month for a Supabase Pro plan, not five figures."

**Follow-up:** "What if EMTA wanted to host this themselves?"
> "Everything is open source under the GitHub repo. Supabase has a self-hosted Postgres option, the dashboard is one Streamlit script, the cron is one YAML file. I'd help with the migration. Or if you'd prefer I keep running it as a public service, that's also an option — I built it for the city, not for myself."

---

### Q7. "What does this tell us that we don't already know from our own internal reports?"

**Why they ask:** This is the punch in the stomach. If they have an internal AVL dashboard, why does yours matter?

**Your answer:**
> "Three things, honestly.
>
> **One — public visibility.** Whatever EMTA has internally, riders don't see it. This dashboard is public. A rider, a city council member, or a journalist can right now go to a URL and see Friday afternoon route 5 reliability over the past month. That's a different conversation than 'send me last quarter's PDF.'
>
> **Two — granularity.** I don't know what your internal cadence is, but most agency reports I've seen are monthly or quarterly. Mine is per route, per hour-of-day, per day-of-week, refreshed every five minutes. That's a different question. It's not 'how was route 5 in March,' it's 'is route 5 systematically late at 5 PM on Fridays.' The first question gets a board report. The second one gets a schedule fix.
>
> **Three — natural language summaries.** The Claude digests turn the numbers into something a non-technical reader can act on. A council member can read 'OTP dropped 4 points yesterday with route 14 the worst at 58%' and ask the right follow-up. They can't ask the right follow-up from a heatmap.
>
> If your internal tools already do all three, this is at most a complement, not a replacement. But I haven't seen a rider-facing one in any small or mid-size US transit agency, and Erie is the size of agency where 'we should have one' usually loses to 'we don't have the budget for it,' which is exactly why I built this on free tiers."

---

### Q8. "Why should we trust a college student's pipeline over a vendor with insurance and SLAs?"

**Why they ask:** The hardest question, and they will ask it. The right move is not to defend yourself — it's to agree with the framing and reframe the choice.

**Your answer:**
> "You shouldn't, for anything mission-critical. If a dispatcher needs to make a routing decision in the next 30 seconds, they should use the system EMTA already paid for. What I built is a public-facing analytics layer that runs alongside your operations, not inside them. Nothing I do can affect a bus, a schedule, or an AVL feed — the data flow is one-directional, read-only.
>
> The trust question is therefore narrower: do you trust the *numbers* on a public dashboard to be honest? That's a question I can answer with the code. Every formula is in a file you can read, every aggregation is in SQL you can audit, every AI digest is built on numbers you can verify. The whole repo is on GitHub. If you want to fork it, audit it, change the threshold from −1/+5 to your own values, point it at a different API — you can. That's a different posture than a vendor product, and for the rider-facing use case I think it's the right one."

---

### Q9. "What happens to this if you graduate, get a job, lose interest?"

**Your answer:**
> "Honest answer: the system keeps running until something breaks. The cron is automated, the dashboard auto-deploys from `main`, the AI digests are cache-first so they don't burn budget when no one's watching. The free tiers don't expire.
>
> But you're right that one person isn't a maintenance plan. Three options:
>
> 1. **EMTA forks it.** Take the repo, run it on agency infrastructure, change the README. I'd help with the handoff.
> 2. **A handoff plan.** I document the runbook — there's already a `setup_vm.sh` and a CLAUDE.md — and identify a successor (a CS student at Mercyhurst, Penn State Behrend, Gannon).
> 3. **Status quo with a sunset clause.** It runs as a public good for as long as I can maintain it. If I stop, the repo stays public; anyone can pick it up.
>
> I'd be most comfortable with option 2 because it doesn't put the burden of choosing on you."

---

### Q10. "What's *wrong* with your project? What would you change?"

**Why they ask:** They want to see if you'll volunteer the weaknesses or wait to be caught. Volunteer them.

**Your answer:**
> "Five real limitations.
>
> 1. **5-minute polling.** A bus that was 30 seconds late and then on time again was never seen. For high-frequency routes that's a blind spot.
> 2. **No GTFS schedule integration.** I rely on adherence values from your API. If the API's adherence is stale or wrong on a given trip, my numbers inherit that. With GTFS, I could compute adherence independently and cross-check.
> 3. **No weather, no school calendar, no holidays.** A snowstorm and a normal Tuesday look identical. That makes the AI digests occasionally tone-deaf — 'route 5 was unusually late' on the morning of a six-inch snowfall.
> 4. **No headway metric.** As discussed, OTP isn't the right measure for high-frequency service.
> 5. **The Live Map is a dispatcher tool, not a reliability tool.** I currently show it on the public dashboard, but its value is operational, not analytical. I should either remove it or label it as 'live ops view, not historical performance.'
>
> If I had another month, GTFS integration is the highest-leverage fix because it makes everything else more accurate."

---

## Part 5 — Curveballs they might throw

These are less likely but plan for them.

### "Have you talked to our IT/data team?"
> "No, I built this as a public-facing rider tool using a public API, and I wanted to come to you with a working demo first rather than a proposal in the abstract. If after this conversation you want me to coordinate with your IT or data team, I'd welcome that introduction."

### "Could a competing transit advocate group weaponize this against us?"
> "It's already public — anyone could already build this, the API is open. Putting honest numbers in front of riders isn't a weapon, it's accountability. If the numbers look bad, the right response is to fix the service, not the dashboard. And the AI digests are deliberately neutral in tone — no 'EMTA failed riders,' just 'OTP was X%, the worst routes were Y.' I worked on that prompt for a while."

### "What about ADA / paratransit / equity considerations?"
> "Paratransit isn't in the AVL feed I'm using, so it's not in the analysis. That's a real gap — fixed-route reliability and paratransit reliability are very different conversations, and only one is in this dashboard. I should label that more clearly. On equity: I could add a route-by-route breakdown weighted by ridership of low-income census tracts, but I don't have the ridership data. If EMTA shares it, that's a real analysis to add."

### "Do you have testimonials from riders?"
> "Not yet — I built the system before I built the audience. The Streamlit URL is live but I haven't promoted it. I wanted EMTA's reaction first, because if you tell me the numbers are wrong or misleading, I'd rather fix that before riders rely on it."

### "How do you handle PII?"
> "There is none. Vehicle IDs are not personally identifying, no driver names, no rider data. The Avail API doesn't expose passenger counts at the individual-trip level. The only quasi-identifier is the bus number, and that's already on the side of the bus visible to the public."

### "Why didn't you use [insert tool — Tableau / PowerBI / Looker]?"
> "Three reasons: cost (free tier of any of those is more limited than Streamlit), reproducibility (Streamlit is in the repo, anyone can run it locally), and code review (Streamlit is a Python script, it diffs cleanly in git, it doesn't live in a vendor's UI). For a small agency public-facing tool, those mattered. For an internal BI deployment with 50 dashboards, I'd absolutely use a real BI tool."

---

## Part 6 — Things to NOT say

| Don't say | Why |
|---|---|
| "Your data is dirty" | Even if true. Frame as "the API returns some pings I have to filter out." |
| "Your reports are wrong" | You don't know what their reports say. Frame as "if our numbers differ, here's how to find out why." |
| "This is better than what you have" | You don't know what they have. Frame as "this complements public-facing visibility." |
| "I used Claude / AI to build this" | They'll discount the engineering. The AI is a *feature* of the product, not a *builder* of the product. |
| "It only took me a weekend" | Diminishes the work, makes them suspect quality. Just say "I built this over [N] months." |
| "I want to monetize this" | If asked, you can clarify your goals, but don't lead with money. |

---

## Part 7 — What to bring

- **Laptop with the dashboard live**, on EMTA Wi-Fi if possible — pre-load the URL, pre-pick a date that has interesting numbers.
- **Phone hotspot** as backup. Streamlit Cloud goes down occasionally.
- **Printout of one daily digest** — if the dashboard fails, you still have a tangible artefact. Printed digest beats a frozen browser.
- **A single-page handout** with: the dashboard URL, the GitHub URL, your email, and the four numbers (OTP formula, polling cadence, layers, cost).
- **A pen and notepad** — when they raise a concern, write it down in front of them. It signals you're listening, not pitching.

---

## Part 8 — How to close

> "I built this because I missed a bus and got curious. I'm not asking EMTA for anything today — not money, not a contract, not data access I don't already have. What I'd like is fifteen minutes of feedback. Tell me where the numbers look wrong, where the framing is off, what would make it useful internally, and what concerns I haven't anticipated. That's how this gets better. And if it ends up being useful to you, that's the best outcome I can imagine for a project that started with a bus stop and a notebook."

---

## Part 9 — The hardest possible question, prepared

**"This is impressive but you're 22 years old and we have actual operations to run. Why are you in this room?"**

> "Because the alternative was to keep complaining about the bus to my friends, and they didn't have the data either. I'm in this room because I built something that didn't exist, and I think it's better to show it to you than to publish it without showing you. If at the end of this conversation you tell me the most useful thing I can do is take it down or hand it to your IT team, that's a fine outcome. I came to listen."

That's the answer. Memorize that one verbatim.

---

## Appendix — Quick technical reference (if they go deep)

**Stack:**
- Python 3.10+, psycopg2 (no ORM)
- PostgreSQL on Supabase
- Streamlit + Plotly Express for dashboard
- Anthropic Claude API for AI digests
- GitHub Actions cron + Oracle Cloud Free VM crontab
- python-dotenv for secrets locally; encrypted CI secrets; st.secrets in production

**Idempotency guarantees:**
- Bronze: `INSERT ... ON CONFLICT (vehicle_id, observed_at) DO NOTHING`
- Silver: `DELETE WHERE date IN window` then bulk INSERT — atomic per run
- Gold: `INSERT ... ON CONFLICT (route_id, hour_of_day, day_of_week) DO UPDATE`

**Critical bugs I found and fixed (have these ready):**
1. **Timezone bug** — 17 occurrences of `observed_at::date` rewritten to `(observed_at AT TIME ZONE 'America/New_York')::date`. Pings 8–11 PM ET were stamped next-day in UTC.
2. **AI hallucinated system-wide percentages** — added scrub-and-inject post-processing, KPI snapshot freeze.
3. **Route sort order** — `_route_sort_key` so 105 doesn't sort before 3 in the dropdown.
4. **Excluded routes** — 98/99/999 dragged aggregate OTP up artificially because they were never expected to adhere to a passenger schedule.
5. **Hourly OTP chart started at 04:00** — fixed to start at 05:00, the actual service start.

**One-line answers to "what is it":**
- *To a non-technical person:* "A free public website that scores Erie buses for on-time performance and writes a plain-English summary every day."
- *To a technical person:* "A medallion-architecture ETL on Postgres pulling Avail InfoPoint every 5 minutes, with cache-first Claude digests and a Streamlit frontend."
- *To EMTA:* "A rider-facing analytics layer for your existing AVL data that uses the same TCRP 88 metric peer agencies report."

---

## Appendix B — Erie transit study guide (know this cold)

EMTA goes by **"the e"** publicly (`ride-the-e.com`). Founded 1966. Operates fixed route + paratransit (LIFT) in Erie County. **No Sunday service.** ~27 fixed routes, ~1,300 stops, ~5,500 service miles per weekday.

### The full route list (memorize the shapes, not all the numbers)

**Urban core (single-digit and teens, frequent, downtown-radial):**
- **Route 1** — Glenwood
- **Route 3** — Peach Street *(the major north-south spine through downtown)*
- **Route 4** — Liberty Street
- **Route 5** — UPMC Hamot
- **Route 11** — Harborcreek
- **Route 12** — Albion
- **Route 14** — Edinboro
- **Route 15** — E 38th Street
- **Route 16** — North East
- **Route 18** — Penn State Behrend *(university)*
- **Route 19** — Gannon *(university)*

**Downtown loops (20-series, short circulators):**
- **Route 20A** — Downtown Loop
- **Route 20L** — Cultural Loop
- **Route 21** — Lawrence Park
- **Route 22** — Tacoma
- **Route 23** — Belle Valley
- **Route 24** — McClelland
- **Route 25** — Wesleyville
- **Route 26** — E 26th Street *(this is the one near Behrend; not "always late" — it's a short urban route)*
- **Route 27** — State Street
- **Route 28** — Erie Heights
- **Route 29** — Asbury
- **Route 30** — West Millcreek
- **Route 31** — Frontier
- **Route 32** — Westlake

**Long-haul (3-digit, regional, structurally prone to delay):**
- **Route 105** — Corry *(long out-and-back, single-digit OTP swings expected)*
- **Route 229** — Fairview
- **Route 260** — East County
- **Route 261** — West County *(rural long-haul; if your dashboard shows it consistently late, that's structural — long routes accumulate adherence drift)*

**Seasonal / special:**
- **Route 33** — Presque Isle Express *(summer-only, beach service)*
- **PennWest Edinboro Express** *(university)*
- **Morning Tripper / Afternoon Tripper** *(school-session-only, K–12 commuter trippers — these are EMTA's "98 / 99")*

### What the 98, 99, and 999 codes actually are

These aren't published routes that a rider can board with a schedule — they're **internal operational codes** emitted by the AVL system:

- **98 — AM Tripper.** "Tripper" in transit terminology means a school-bell-aligned trip operated as part of the public network (FTA's "Tripper Rule" allows agencies to serve school commutes only if the service is open to the general public). EMTA's AM Tripper covers school dropoff windows.
- **99 — PM Tripper.** Same idea, school pickup windows.
- **999 — Deadhead.** A non-revenue trip — a bus repositioning between assignments (e.g., garage → first stop, or end-of-route → another route's start). Industry term: "dead mileage" or "deadrunning." No riders, no schedule, no adherence.

**Why I exclude them from analytics:** None of these have a public schedule riders are timing themselves against, so OTP for them is meaningless. Including them in the aggregate would either inflate or deflate the system OTP depending on how the AVL system reports adherence for non-revenue trips, and either way it'd be a number nobody could act on. The exclusion is in `ingestion/config.py` and is filtered consistently in Silver, Gold, the dashboard, and the AI prompts.

**If EMTA asks why your dashboard doesn't show trippers:** "They're not passenger routes with a public schedule — riders can't time themselves against them, so OTP for them isn't a meaningful number. If you want a separate operational view that includes trippers and deadheads, that's a one-line config change."

### Routes a rider would call out as "always late"

You mentioned **261**. Before claiming "this route is always late," check the data — but here's the framework:

- **Long routes (105 Corry, 229 Fairview, 260/261 County)** are structurally prone to adherence drift. A 90-minute one-way trip has 18× more chances to fall behind than a 5-minute downtown loop. Industry-wide, OTP is lower on long routes. Don't be surprised if 105/261 are in your worst-routes list every digest.
- **Routes with heavy ridership through congested corridors** (3 Peach, 5 Hamot) absorb dwell-time variability at every stop. Their OTP usually beats long-haul routes but trails low-ridership feeders.
- **Trippers and university routes (18 Behrend, 19 Gannon, PennWest)** see massive demand spikes at class-change times. Dwell variability there is mostly a Behrend/Gannon problem, not an EMTA problem.

**The right framing for the meeting:**
> "The dashboard surfaces the worst-performing routes, but 'worst' usually means 'longest' — that's a known industry pattern. The interesting question isn't 'which route is worst,' it's 'which route is worse than its peer group.' A 5-minute downtown circulator at 78% OTP is a problem; a 90-minute county route at 78% is normal. I haven't built peer-group normalization yet — that's a real next step."

---

## Appendix C — TCRP Report 88 in plain language

**Full title:** *TCRP Report 88: A Guidebook for Developing a Transit Performance-Measurement System.* Published 2003 by the Transportation Research Board's Transit Cooperative Research Program. ~300 pages.

**What it is:** Not a federal mandate, not a regulation. It's the **canonical reference** that US transit agencies cite when they explain how they measure their own performance. It standardized the language ("on-time performance," "headway adherence," "service reliability") and the formulas, so an OTP number from EMTA can be compared to one from TriMet without arguing about definitions.

**The OTP formula (the one I use):**

```
OTP% = (on-time observations / total observations) × 100
```

That is literally it. No weighting, no penalty score, no logarithm. Count the pings inside the on-time window, divide by all the pings, multiply by 100.

**The "on-time window" is the only knob.** TCRP 88 originally suggested **0 minutes early to +5 minutes late** as a default. Modern practice has shifted earlier on the early side because riders showing up at the scheduled time get stranded by early buses. Different agencies pick different windows:

| Agency | City / Region | Size | On-time window | Notes |
|---|---|---|---|---|
| **WMATA** | Washington, DC | Very large (700+ buses) | −2 min / +7 min | Wider late tolerance; high-frequency network |
| **TriMet** | Portland, OR | Large (~100 bus routes) | −1 min / +5 min | The threshold this dashboard uses |
| **AC Transit** | Oakland / Bay Area | Large (~60 routes) | −1 min / +6 min | Slightly looser late side than TriMet (source: actransit.org service reliability page) |
| **MBTA** | Boston, MA | Very large | −1 min / +6 min | Same as AC Transit |
| **EMTA (this dashboard)** | Erie, PA | Small (27 routes) | −1 min / +5 min | EMTA publishes no official OTP window — I chose TriMet's as the strictest common standard; it can be changed in one config line |

### What are these agencies — plain English

- **WMATA** (Washington Metropolitan Area Transit Authority) — runs DC's Metro subway *and* the Metrobus surface network. One of the 5 biggest transit systems in the US. Their OTP window (−2/+7) is looser because their network is enormous and frequency is high — a rider waiting 2 minutes early isn't really stranded the way they would be on a 30-minute headway rural route.
- **TriMet** — Portland, Oregon's regional transit agency. Runs buses and the MAX light rail. Known for high transparency — they publish monthly performance reports publicly. Their −1/+5 window is considered the industry "strict" standard.
- **AC Transit** — Alameda-Contra Costa Transit District, serving Oakland and the East Bay. Uses −1/+6. Their published OTP goal is 72% — a useful benchmark for EMTA: if Erie is at 70%, they're within shouting distance of a well-regarded mid-size agency.
- **MBTA** — Massachusetts Bay Transportation Authority, Boston. Very large, includes commuter rail. Uses −1/+6 for bus.
- **EMTA** — Erie's agency. 27 routes, no Sunday service, ~5,500 service miles per weekday. Small agency by national standards, which means less slack in the schedule and higher sensitivity to any single vehicle problem. **EMTA does not publish its OTP window publicly.** This is actually a question worth asking them: "What does EMTA consider on-time?" If they use 0/+5, my numbers will be slightly lower (I penalize early buses). If they use −1/+7, my numbers will be slightly higher. Either way, the formula is the same.

**What this means for the EMTA presentation:** Don't say "WMATA uses the same formula as me." Say: "**All major US agencies use the same OTP formula from TCRP 88. The only difference is which window they call on-time. I use −1 to +5, the same as TriMet. EMTA can change that window to whatever they use — it's one config line.**" That's the truthful, confident version.

---

## Appendix D — The medallion architecture in 60 seconds

The dashboard reads from a database that has three **layers**, each one progressively cleaner. The pattern is called "medallion architecture" (Bronze, Silver, Gold) — coined by Databricks, now widely used in the data world.

### Bronze — `bronze_vehicle_pings`
**The raw dump.** Every single API response from Avail InfoPoint gets stored exactly as received, including a full `raw_json` column with the original payload.

- **Purpose:** "Lose nothing." If a bug ever corrupts the layers above, Bronze is the source of truth and we rebuild upward.
- **Idempotency:** `INSERT ... ON CONFLICT (vehicle_id, observed_at) DO NOTHING`. If the same ping arrives twice (because both cron runners fired), the second insert is a no-op.
- **Size:** Largest layer. Pruned by storage usage, not age, to stay under Supabase's 500 MB free tier ceiling.

### Silver — `silver_arrivals`
**The cleaned version.** One row per ping, but transformed:

- UTC timestamps converted to America/New_York (Eastern Time)
- Parked buses filtered out (`speed < 2 mph` excluded)
- Each ping tagged with a **delay bucket** (`early`, `on_time`, `late`, `very_late`) based on the adherence value and the on-time window in `ingestion/config.py`
- Excluded routes (98, 99, 999) dropped here

- **Purpose:** Anything downstream can aggregate however it wants — by hour, by day, by route, by vehicle — without re-doing timezone math or re-classifying pings.
- **Idempotency:** Delete-then-reinsert per date window. Running Silver twice for 2026-05-06 wipes that day's rows and rewrites them. No duplicates possible.

### Gold — `gold_route_reliability`
**The rollup.** Aggregated by `(route_id, hour_of_day, day_of_week)`, with OTP% computed per bucket.

- One row per (route, hour, day-of-week) combination — e.g., one row for "Route 5, 5 PM, Friday."
- **No date column.** Gold is a *lifetime* aggregate. Every run rewrites it.
- **Idempotency:** `INSERT ... ON CONFLICT (route_id, hour_of_day, day_of_week) DO UPDATE`. Each row gets overwritten with the latest counts.
- **Purpose:** Sub-second responses for the dashboard. The query "what's Route 5's average OTP at 5 PM on Fridays for all time" reads ONE row from Gold instead of scanning millions of Silver rows.

**The big idea:** Each layer has one job. If Silver has a bug, Gold is wrong but Bronze is intact, so I rebuild. If I want to change the on-time window from −1/+5 to 0/+5, I rerun Silver, then rerun Gold. Bronze never moves.

---

## Appendix E — The OTP formula, with your example

> "If there are 800 pings and 600 are on time, the formula is just 600 / 800 × 100?"

**Yes. Exactly that.** That's 75% on-time performance. There is nothing more sophisticated happening.

**Worked example for a single route-hour cell in Gold:**

Route 5, hour 17 (5 PM), Friday, looking at every Friday 5 PM ping ever recorded:

| Bucket | Count | Definition |
|---|---|---|
| `early` | 40 | More than 1 min early |
| `on_time` | 600 | −1 to +5 min |
| `late` | 130 | +5 to +15 min |
| `very_late` | 30 | More than +15 min |
| **Total** | **800** | |

```
OTP% = 600 / 800 × 100 = 75.0%
```

Gold stores this row as: `route_id=5, hour_of_day=17, day_of_week=5 (Friday), on_time_count=600, total_pings=800, reliability_score=75.0`.

**Why I don't use a "penalty score" instead:**
A "weighted" score (e.g., very_late counts as −2, late as −1, on_time as +1) feels more sophisticated but it's not benchmarkable. EMTA's 78.4% becomes meaningless if it can't be compared to TriMet's 78.4%. The whole point of using TCRP 88 is interoperability of the number across agencies.

**Why I use pings, not trips:**
A "trip" is a scheduled run from start to end (e.g., the 5:15 PM Route 5 from Downtown to Hamot). A "ping" is a single GPS report from a moving bus. I don't have GTFS schedule data to identify which trip a ping belongs to, so I score every ping independently. The trade-off: a trip that's 10 min late at the start, on-time in the middle, 10 min late at the end shows up as ~33% on-time in my system, even though the rider's experience was "the bus was late." With GTFS integration, I could score per-trip-arrival-at-timepoint, which is what TCRP 88 actually recommends. **This is a real limitation, name it before they do.**

### How to present the formula in the meeting (one-liner)

> "On-time performance is just: count the pings that landed inside the on-time window, divide by all the pings, multiply by 100. If there are 800 pings on Friday at 5 PM and 600 of them were within −1 to +5 minutes of schedule, that's 75% OTP. Same arithmetic that TriMet, AC Transit, and WMATA use — they just pick slightly different windows."

That's the whole pitch. Don't overcomplicate it.

---

*Last updated: 2026-05-07. Update before the meeting if the dashboard or pipeline changes.*

## Sources

- [EMTA — Routes](https://ride-the-e.com/routes-2/)
- [EMTA — Erie Metropolitan Transit Authority homepage](https://ride-the-e.com/)
- [Erie Metropolitan Transit Authority — Wikipedia](https://en.wikipedia.org/wiki/Erie_Metropolitan_Transit_Authority)
- [EMTA — Fixed route changes January 10, 2026](https://ride-the-e.com/2025/12/10/fixed-route-changes-january-10th-2026/)
- [TransitWiki — Tripper Rule](https://www.transitwiki.org/TransitWiki/index.php/Tripper_Rule)
- [Wikipedia — Dead mileage](https://en.wikipedia.org/wiki/Dead_mileage)
- [Human Transit — Basics: dead running](https://humantransit.org/2011/06/dead-running.html)
- [TCRP Report 88 — A Guidebook for Developing a Transit Performance-Measurement System (full PDF)](https://onlinepubs.trb.org/onlinepubs/tcrp/tcrp_report_88/guidebook.pdf)
- [TCRP Report 88 — Summary](https://onlinepubs.trb.org/onlinepubs/tcrp/tcrp_report_88/SummaryDoc.pdf)
- [WMATA — Bus Service Guidelines (Metrobus, Dec 2020)](https://www.wmata.com/initiatives/plans/upload/Final-MetroBus-Service-Guidelines-2020-12.pdf)
- [TransitCenter — "Your Bus Is On Time. What Does That Even Mean?"](https://transitcenter.org/bus-time-even-mean/)

---

## Appendix G — Caveman explanations (for concepts you're fuzzy on)

These are plain-English expansions of the technical talking points in this doc. Read these until the concept clicks, then go back and read the sharp version above.

---

### 1. "Why not headway-based reliability for high-frequency routes?"

Imagine a bus that comes every 8 minutes. You don't check the schedule — you just walk to the stop and wait. You don't care if the 8:04 bus came at 8:06. You care if you waited 20 minutes instead of 8.

OTP (on-time performance) measures whether a bus hit its scheduled time. That only makes sense when riders are actually timing themselves against a schedule — like "the 8:04 comes once an hour, I need to be there at 8:03." For a bus that comes every 8 minutes, nobody does that. Nobody sets an alarm for 8:04 when there's another bus at 8:12.

For those frequent routes, the right question is: did buses come evenly spaced, or did two buses bunch together leaving a 16-minute gap? That's called headway adherence — measuring the gap between buses, not the gap from a scheduled time. If EMTA runs a route every 8 minutes but two buses show up back-to-back with a 16-minute hole behind them, OTP might say "both buses were on time" (they hit their individual scheduled times), while riders experienced a 16-minute wait. OTP would lie. Headway wouldn't.

I don't have this built. My system scores every route the same way with OTP. For EMTA's lower-frequency routes (30–60 min headways), that's fine — riders absolutely check the schedule. For any route running every 10 minutes or less, my numbers are less meaningful and I should say so.

---

### 2. "What happens when the API goes down?" — plain version

Think of the pipeline like a factory assembly line. Raw materials (GPS pings) come in one end, and finished products (reliability scores) come out the other. What happens if the supplier stops delivering?

**Thing 1 — the factory doesn't burn down.** The code that fetches data is wrapped in a try/except block. In plain English: if the API doesn't respond, the code says "okay, nothing to do" and exits cleanly instead of crashing and sending error emails at 3 AM. The next run 5 minutes later tries again. No alarm, no drama.

**Thing 2 — when the supplier comes back, there are no duplicates.** The database is designed so that writing the same data twice produces the same result as writing it once. Bronze ignores a ping it's already seen (same bus, same timestamp = do nothing). Silver wipes the day's rows and rewrites them fresh every run, so running it twice just gives you the same rows twice — not double the rows. Gold overwrites the same row with updated counts every run. This property is called idempotency — it means "running it again doesn't break anything."

**Thing 3 — the dashboard just shows less, not wrong.** If 2 hours of pings are missing because the API was down, the OTP for those hours uses a smaller denominator. Instead of 200 pings, maybe only 80 were captured. The percentage might be slightly off, but it won't show a fabricated number. The gap is visible in the hourly chart — a thin bar where there should be a tall one — which is honest.

---

### 3. "The JSONB snapshot freeze + scrub-and-inject" — plain version

**The snapshot freeze:**

Every time Claude writes a daily digest, the code takes a photo of all the numbers Claude was shown — total pings, OTP%, worst routes, hourly breakdown — and saves that photo (called a JSONB blob) to the database alongside the narrative text. When you load the dashboard later, both the narrative and the KPI numbers you see on screen come from that same saved photo. They can't disagree because they're literally the same source. Without this, you'd have Claude writing "OTP was 74%" in the narrative while the live database had already updated to 71% — and readers would see both numbers on the same screen, one wrong. That happened. This fix prevents it.

**The scrub-and-inject:**

Claude kept making up numbers. You'd give it "OTP was 72.2%" and it would write "the system ran at approximately 73.8% on-time" in the digest — close but wrong, and confidently stated. The instruction "don't invent percentages" in the prompt didn't work; Claude ignored it. So instead of trusting Claude with numbers, the code now acts as an editor after Claude finishes. It reads Claude's output, finds any paragraph that looks like a stats summary (sentences with percentages, phrases like "system-wide" or "overall performance"), deletes that paragraph, and replaces it with a sentence the code wrote itself directly from the snapshot — something like "System OTP for May 6 was 72.2% across 14,203 pings." That sentence is built by Python, not Claude, so it's always right. Claude gets to write the narrative flavor and route-level observations; it just can't touch the headline numbers.

---

### 4. Idempotency guarantees — plain version

Idempotency means: if you do the same thing twice, you get the same result as doing it once. Like pushing an elevator button — pushing it again doesn't summon two elevators.

**Bronze** uses "insert, but if this exact row already exists, do nothing." Same bus, same timestamp, already in the table? Skip it. This handles the case where both cron runners (GitHub Actions and the Oracle VM) fire at the same time and both try to insert the same ping.

**Silver** uses "delete everything for this date, then insert fresh." If Silver runs twice for May 6, the second run first wipes all May 6 rows, then writes them again from scratch. You end up with the same rows either way — not double. This approach is simple and safe: there's no clever "update if exists" logic, just a clean wipe and rewrite.

**Gold** uses "insert this row, but if the route/hour/day combination already exists, overwrite it with the new counts." So running Gold twice just overwrites the same row with the same values. The table always reflects the latest full calculation, never accumulates duplicates.

---

### 5. Critical bugs — plain version

**Bug 1 — Timezone.** Computers store time in UTC (London time, basically). Erie is UTC−5 in winter, UTC−4 in summer. A ping that happened at 9 PM Erie time is actually 1 AM the next day in UTC. The original code converted time to a date using UTC, so every ping between 8 PM and midnight Erie time got labeled as "tomorrow." This made the nightly numbers look weirdly low (because half the evening was credited to the next day) and the early morning numbers look inflated. Fixed by always converting to Eastern Time before extracting the date. Found in 17 places in the code.

**Bug 2 — Claude inventing numbers.** Described above in the scrub-and-inject section. Short version: Claude saw 72.2%, wrote 73.8%. Added code that strips Claude's number paragraphs and inserts Python-computed ones instead.

**Bug 3 — Route sort order.** Computers sort text alphabetically by default. Alphabetically, "105" comes before "3" because "1" < "3". So the route dropdown showed: 1, 105, 11, 12, 14... instead of 1, 3, 4, 5, 11, 12... Fixed by adding a sort function that treats route IDs as numbers, not strings.

**Bug 4 — Routes 98/99/999 inflating OTP.** These routes (AM Tripper, PM Tripper, Deadhead) don't have a real passenger schedule. The AVL system still reports an "adherence" value for them, but it's meaningless — nobody is timing them against a schedule. When included in the aggregate, they added thousands of pings that were often "on time" (because the API reports 0 adherence for non-revenue trips, which falls inside the on-time window). This made the system-wide OTP look better than it actually was. Fixed by excluding them at the Silver layer.

**Bug 5 — Chart starting at 4 AM.** The hourly OTP chart showed an hour-of-day axis starting at 04:00. EMTA service doesn't start until 5 AM. The 4 AM bar was either empty or contained noise from buses warming up / repositioning with no passengers. Fixed to start at 05:00.

---

### 6. GTFS integration — why it's the highest-leverage fix

GTFS stands for General Transit Feed Specification. It's a standardized file format that transit agencies use to publish their schedules — every route, every stop, every scheduled arrival time, in a format that Google Maps and other apps can read. EMTA almost certainly has a GTFS feed (it's how Google Maps knows when buses are supposed to arrive).

Right now, this dashboard doesn't use GTFS at all. It relies entirely on the Avail InfoPoint API, which reports each bus's current adherence — the API says "this bus is 3 minutes late." The dashboard trusts that number. The problem is: late compared to what? The API's adherence value is computed by Avail's software against their internal copy of the schedule. If Avail's schedule is stale, wrong, or uses different rounding, the adherence is wrong — and all the OTP numbers inherit that wrongness.

With GTFS, the dashboard would have its own independent copy of the schedule. Instead of asking "what does the API say this bus's adherence is?", it would ask "when was this bus supposed to be at this stop, and when did it actually get there?" — computing adherence itself from raw GPS position + schedule data. That's what TCRP 88 actually recommends (scoring per trip, per timepoint). It would also unlock headway calculations (you know the scheduled headway from GTFS, so you can compare actual gap vs. scheduled gap), trip-level scoring instead of ping-level scoring, and cross-checking against the API's own adherence values to catch API errors.

Every limitation in Q10 of the mock interview either directly requires GTFS or becomes easier to fix once GTFS is in. It's the foundation the rest of the improvements would be built on.

---

**What the document is:** A 24-page summary of the full ~300-page Guidebook. Published by the Transportation Research Board / National Research Council. Covers how to build a transit performance-measurement program from scratch.

### Why agencies measure performance (3 reasons)
1. Required to (NTD reporting to FTA)
2. Useful internally (monitor service, evaluate performance, make decisions)
3. External accountability (board, public, funding bodies)

### 4 performance points of view
- **Customer** — quality of service as riders perceive it
- **Community** — transit's impact on mobility, economy, environment
- **Agency** — efficiency and effectiveness of operations
- **Driver/Vehicle** — speed, delay, headway (indirectly reflects customer experience)

### 8 performance measure categories
1. Availability — where/when service runs
2. **Service delivery — reliability, OTP, customer service** ← most relevant to this project
3. Safety and security
4. Maintenance and construction
5. Economic — cost efficiency, cost effectiveness, ridership
6. Community — social/economic impact
7. Capacity — ability to move people
8. Travel time

### Most commonly used measures (50%+ of agencies)
- Cost effectiveness
- **Ridership**
- **On-time performance** ← the one this project implements
- Cost efficiency
- Accident rate

### The OTP formula (from the document)
The document lists "on-time performance" as a core Service Delivery measure for agencies of *all* sizes (small, medium, large). The formula is never more complex than:

```
OTP% = (on-time observations / total observations) × 100
```

The document endorses multiple standard-setting methods — comparison to industry peers, trend analysis, self-identified targets. Using TriMet's −1/+5 window is the "identify typical industry standards" method, explicitly recommended.

### 8-step program development process
1. Define goals and objectives
2. Generate management support
3. Identify users, stakeholders, constraints
4. Select performance measures and develop consensus
5. Test and implement
6. Monitor and report
7. Integrate results into decision-making
8. Review and update

### Core fixed-route measures for small agencies (relevant EMTA size)
- **Availability:** Route coverage
- **Service delivery:** Missed trips, complaint rate, on-time performance, passenger load
- **Safety:** Accident rate, vandalism incidents
- **Economic:** Ridership, productivity, cost effectiveness, cost efficiency

### Key quote to remember
> "What gets measured, gets attention. Conversely, what isn't measured, doesn't get acted upon."

### What to say in the meeting about this document
> "TCRP 88 is the canonical reference — not a regulation, not a mandate, just the standard that US transit agencies cite when explaining how they measure themselves. The OTP formula it defines is used by WMATA, TriMet, AC Transit, MBTA. The only variable is the on-time window. I use TriMet's window because it's the most commonly cited strict standard for a fixed-route system. EMTA can change it to whatever window they use internally — it's one config line."

---

## Appendix H — Recent build log (May 2026)

Two substantial changes shipped in the same week. Documenting both here in case EMTA asks "what's actually new" or "show me a change you made and why."

### H.1 — Route Corridor map (replaces the old Live Map / Activity-by-Route)

**The problem.** The original Live Map tab had two views: a live dot map and a per-route "activity heatmap" colored by route number. The activity view tried to render 27 routes simultaneously on one map. Twenty-seven colors is more than the human eye can distinguish — riders couldn't tell which dot was Route 22 vs Route 26. Even the dispatcher couldn't read it.

**The redesign.** Route Corridor view, one route at a time:
- Pick a route from a dropdown.
- The map draws the route's actual road geometry (from EMTA's published GTFS feed) as a gray polyline — the route "spine."
- On top of the spine, every ~100m stretch of road is colored by **average adherence** through the chosen date range, using four discrete buckets: green (on-time), yellow-green (slightly late), amber (late), red (very late). Four colors the eye can resolve, instead of 27.
- A "Top 5 late stretches" table sits next to the map with the nearest GTFS stop name on each row ("W 12th & Selinger Ave"), the average delay, the ping count, and which bus IDs were observed there.
- A companion table lets the user pick a specific bus (vehicle_id) and re-color the map for that bus only. Useful when 2–3 buses pass through the same corridor on the same day and one of them is responsible for the red stretch.

**What had to change in the database to support this.** EMTA's realtime feed gives us GPS positions but no road geometry. Without GTFS, sparse routes (16, 105, 261 — once or twice a day) have so few pings that the corridor is invisible. So I integrated GTFS:
- New table `gtfs_shapes` — every route's road geometry as a sequence of lat/lon points.
- New table `gtfs_trips` — maps `route_id → shape_id` so we know which shape belongs to which route.
- New table `gtfs_stops` — every physical bus stop with its name and coordinates, used for "nearest stop" labels via a LATERAL JOIN.
- New ingestion script `ingestion/load_gtfs.py` that downloads EMTA's published GTFS zip (`https://emta.availtec.com/InfoPoint/GTFS-Zip.ashx`), parses the three CSVs, and bulk-loads them via `psycopg2.extras.execute_values`.
- New GitHub Actions workflow `gtfs_weekly.yml` — runs Monday 03:00 ET to refresh GTFS (it changes a few times a year, weekly is plenty).

**Why GTFS instead of inferring geometry from ping positions.** Pings are sparse and noisy. The official feed is the ground truth EMTA already publishes. Using it costs nothing, it's the same source GTT, Transit, and Google Maps consume, and it lets the corridor render correctly even when there's only one ping in a particular cell.

**One small bug I caught during this work.** Setting `st.session_state["corridor_bus_table"] = None` to clear the selection corrupted Streamlit's widget state and crashed the app with `TypeError: ReadOnlyAttributeDictionary`. The fix was `st.session_state.pop("corridor_bus_table", None)` — Streamlit wants the key gone, not set to None. Saved as a one-line lesson.

### H.2 — Silver filter cleanup (Option D)

**The setup.** Every analytics query in the codebase — dashboard, daily agent, weekly agent — applies `WHERE speed > 2` to filter out parked buses. The intent is "only count pings where the bus is in motion, so we're measuring actual service, not idle telemetry."

**Where the question came from.** A reasonable challenge during meeting prep: a bus stuck in heavy traffic at 0 mph is still in service and falling behind schedule, but `speed > 2` throws those pings away. Proposed fix: "only exclude if a bus is stationary for more than 8–10 minutes."

**Why a duration heuristic was the wrong answer.** Two reasons:
1. The duration approach can't distinguish a scheduled terminal layover (10 minutes stopped, that's the published schedule) from a traffic jam (10 minutes stopped, that's a service failure). Both look identical to a duration filter.
2. EMTA's feed already publishes `op_status` — the values are `ONTIME`, `LATE`, `EARLY`, `TRIP START`, `LOGGED IN`. The last one is "driver clocked in, not on a trip." Using that field is more accurate than re-inventing it from timestamps.

**The audit that decided the change.** Before flipping anything I ran four candidate filters against the same 7 days of Bronze (30,286 pings):

| Filter | Pings kept | System OTP% |
|---|---|---|
| A: current (`speed > 2`) | 17,889 | 71.58% |
| B: `op_status <> 'LOGGED IN'` only | 28,349 | 72.28% |
| C: `op_status IN ('ONTIME','LATE','EARLY')` | 24,720 | 70.87% |
| D: `speed > 2 AND op_status <> 'LOGGED IN'` | 17,530 | 71.80% |

Per-route deltas under Filter B were as large as ±3.3 points (Route 28: 79.24% → 82.56%). That would have meaningfully re-ranked the network.

A dedup check explained why: 42% of parked pings were preceded by another parked ping from the same bus within 7 minutes — the same dwell sampled twice. The `speed > 2` filter accidentally deduplicates by motion. Removing it pulls in ~4,700 redundant samples per week, mostly at on-time stops, inflating OTP%.

**Decision.** Option D: keep `speed > 2` for dedup-by-motion, *also* require `op_status <> 'LOGGED IN'` to drop the small number of pings where a moving bus is between trips. System OTP moved by +0.22% — trend continuity preserved — and every per-route shift was under 1 point.

**Implementation.** Single one-line change in `transform/silver.py` (the Bronze→Silver boundary). Every downstream consumer — five sites in `dashboard/app.py`, four in `ai_agent/insights.py`, one in `ai_agent/daily_insights.py` — keeps its existing `speed > 2` filter and inherits the cleanup automatically. After deploying, I re-ran the full 22-day Silver backfill so trend charts don't mix two filter rules.

**What to say if asked.** "I had a hypothesis that the speed filter was throwing away buses stuck in traffic. Before changing anything I ran a side-by-side audit on the same week of raw data under four candidate rules. Two of them looked appealing in theory but shifted per-route OTP by up to three points and double-counted dwell time at stops. The version I shipped keeps the metric stable while removing the small number of out-of-service pings the old rule missed. The audit script is in the repo if you want to re-run it after I'm gone."
