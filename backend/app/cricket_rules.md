# Cricket Rules Specification

**Version:** 1.0
**Status:** Draft
**Owner:** Analytics / NL2SQL
**Scope:** IPL ball-by-ball analytics using the current PostgreSQL schema

---

## 1. Purpose

This document defines the canonical cricket-statistics rules for analytics, SQL generation, and ranking logic over the IPL schema.

It standardizes:

- metric formulas
- dismissal attribution
- legal delivery rules
- batting / bowling / fielding aggregation rules
- eligibility logic
- phase definitions
- schema column mappings
- all-rounder ranking logic
- SQL correctness constraints for NL2SQL systems

This specification is intended to prevent inconsistent metric definitions and invalid query generation.

---

## 2. Schema Scope

This specification applies to the following tables:

- `deliveries`
- `matches`
- `players`
- `playing_xi`
- `replacements`
- `wicket_fielders`
- `teams`
- `team_aliases`

---

## 3. Guiding Principles

1. **Use schema-aware formulas.** Never assume columns that do not exist.
2. **Use role-correct grouping.** Batting aggregates by batter, bowling by bowler, fielding by fielder.
3. **Prefer positive inclusion for dismissal logic.** Bowler wickets must be explicitly defined by dismissal type.
4. **Resolve rankings within a match window.** All metrics, normalization, and eligibility must be computed from the same selected match set.
5. **Do not use arbitrary heuristics unless explicitly labeled.** For example, `runs + wickets * 20` is not a canonical all-rounder ranking rule.
6. **Protect all divisions.** Use `NULLIF()` or explicit `CASE` to avoid divide-by-zero errors.

---

## 4. Canonical Schema Mappings

### 4.1 Fact Table

Use `deliveries` as the ball-by-ball fact table.

### 4.2 Match Metadata

Use `matches` for:

- `season`
- `year`
- `date`
- `venue`
- `city`
- `team1`
- `team2`
- `winner`
- `player_of_match`
- `match_stage`

### 4.3 Player Metadata

Use `players` for:

- `player_name`
- `player_full_name`
- `bat_style`
- `bowl_style`
- `is_keeper`
- `is_occasional_keeper`

### 4.4 Participation Sources

A player may be considered present in a match if they appear in any of:

- `playing_xi.player_name`
- `replacements.player_in`
- `deliveries.batsman`
- `deliveries.non_striker`
- `deliveries.bowler`
- `deliveries.player_dismissed`
- `wicket_fielders.fielder_name`

---

## 5. Mandatory Derived Columns

These derived fields must be used consistently in analytics and SQL generation.

### 5.1 Total Runs on Ball

There is no `total_runs` source column in the schema.

```sql
total_runs = COALESCE(batsman_runs, 0) + COALESCE(extras, 0)
```

### 5.2 Legal Delivery

A legal delivery is any ball that is not a wide and not a no-ball.

```sql
legal_ball = NOT is_wide AND NOT is_no_ball
```

### 5.3 Ball Faced by Batter

A ball counts as faced by the batter unless it is a wide.

```sql
ball_faced_by_batter = NOT is_wide
```

### 5.4 Bowler Runs Conceded

Byes and leg-byes are not charged to the bowler.

```sql
bowler_runs_conceded =
    COALESCE(batsman_runs, 0)
    + CASE
        WHEN is_wide OR is_no_ball THEN COALESCE(extras, 0)
        ELSE 0
      END
```

### 5.5 Dot Ball

Default bowling dot-ball rule:

```sql
dot_ball = legal_ball AND total_runs = 0
```

### 5.6 Boundary Flags

```sql
is_four = batsman_runs = 4
is_six  = batsman_runs = 6
```

---

## 6. Phase Definitions

The schema uses zero-based over numbering.

### 6.1 Schema-Based T20 Phase Mapping

- Powerplay: overs 0–5
- Middle: overs 6–14
- Death: overs 15–19

### 6.2 User-Facing T20 Phase Mapping

- Powerplay: overs 1–6
- Middle: overs 7–15
- Death: overs 16–20

### 6.3 Canonical SQL Phase Expression

```sql
CASE
  WHEN over BETWEEN 0 AND 5 THEN 'powerplay'
  WHEN over BETWEEN 6 AND 14 THEN 'middle'
  ELSE 'death'
END
```

---

## 7. Dismissal Attribution Rules

### 7.1 Bowler Wicket Types

The following dismissal kinds count as wickets credited to the bowler:

- `bowled`
- `caught`
- `caught and bowled`
- `lbw`
- `stumped`
- `hit wicket`

**Canonical Rule:**

```sql
dismissal_kind IN (
  'bowled',
  'caught',
  'caught and bowled',
  'lbw',
  'stumped',
  'hit wicket'
)
```

### 7.2 Non-Bowler Wicket Types

The following do not count as wickets credited to the bowler:

- `run out`
- `retired hurt`
- `retired out`
- `obstructing the field`

### 7.3 Batter Outs

For batting average, a batter is considered out when:

- `player_dismissed IS NOT NULL`
- and `dismissal_kind <> 'retired hurt'`

---

## 8. Batting Rules

### 8.1 Batting Aggregation Grain

Batting metrics must aggregate by batter.

**Correct:**

```sql
GROUP BY batsman
```

**Match-Level:**

```sql
GROUP BY match_id, batsman
```

### 8.2 Batting Innings

A player has a batting innings if they:

- faced at least one ball, or
- were dismissed without facing

### 8.3 Batting Metrics

#### Runs

```sql
runs = SUM(batsman_runs)
```

#### Balls Faced

```sql
balls_faced = COUNT(*) FILTER (WHERE is_wide = false)
```

#### Outs

```sql
outs = COUNT(*) FILTER (
  WHERE player_dismissed IS NOT NULL
    AND dismissal_kind <> 'retired hurt'
)
```

#### Batting Average

```sql
batting_average = runs / outs
```

#### Strike Rate

```sql
strike_rate = 100.0 * runs / balls_faced
```

#### Runs Per Innings

```sql
runs_per_innings = runs / batting_innings
```

#### Fours / Sixes

```sql
fours = COUNT(*) FILTER (WHERE batsman_runs = 4)
sixes = COUNT(*) FILTER (WHERE batsman_runs = 6)
```

#### Boundary Run Percentage

```sql
boundary_run_pct = 100.0 * ((4 * fours) + (6 * sixes)) / runs
```

#### Phase Strike Rate

```sql
phase_strike_rate = 100.0 * phase_runs / phase_balls
```

---

## 9. Bowling Rules

### 9.1 Bowling Aggregation Grain

Bowling metrics must aggregate by bowler.

**Correct:**

```sql
GROUP BY bowler
```

**Match-Level:**

```sql
GROUP BY match_id, bowler
```

> **Never Do This:** Do not compute bowling wickets grouped by batsman.

### 9.2 Bowling Metrics

#### Legal Balls

```sql
legal_balls = COUNT(*) FILTER (WHERE is_wide = false AND is_no_ball = false)
```

#### Overs Bowled

```sql
overs_bowled = legal_balls / 6.0
```

#### Runs Conceded

```sql
runs_conceded = SUM(bowler_runs_conceded)
```

#### Wickets

```sql
wickets = COUNT(*) FILTER (
  WHERE dismissal_kind IN (
    'bowled',
    'caught',
    'caught and bowled',
    'lbw',
    'stumped',
    'hit wicket'
  )
)
```

#### Bowling Average

```sql
bowling_average = runs_conceded / wickets
```

#### Bowling Strike Rate

```sql
bowling_strike_rate = legal_balls / wickets
```

#### Economy Rate

```sql
economy_rate = 6.0 * runs_conceded / legal_balls
```

#### Wickets Per 24 Balls

Useful T20 indicator:

```sql
wickets_per_24_balls = 24.0 * wickets / legal_balls
```

#### Dot Ball Percentage

```sql
dot_ball_pct = 100.0 * dot_balls / legal_balls
```

#### Phase Economy

```sql
phase_economy = 6.0 * phase_runs_conceded / phase_legal_balls
```

---

## 10. Fielding Rules

### 10.1 Fielding Aggregation Grain

Fielding metrics must aggregate by `fielder_name`.

**Correct:**

```sql
GROUP BY fielder_name
```

**Match-Level:**

```sql
GROUP BY match_id, fielder_name
```

### 10.2 Source

Use `wicket_fielders`.

### 10.3 Fielding Metrics

#### Catches

```sql
catches = COUNT(*) FILTER (
  WHERE wicket_kind = 'caught'
    AND is_substitute = false
)
```

#### Run-Out Involvements

```sql
runout_involvements = COUNT(*) FILTER (
  WHERE wicket_kind = 'run out'
    AND is_substitute = false
)
```

#### Stumpings

```sql
stumpings = COUNT(*) FILTER (
  WHERE wicket_kind = 'stumped'
    AND is_substitute = false
)
```

### 10.4 Substitute Fielders

Default rule: exclude substitute fielders from player fielding metrics.

---

## 11. Participation and Match Presence Rules

A player is counted as having played a match if they appear in any of:

- `playing_xi`
- `replacements.player_in`
- batting events
- bowling events
- dismissal events
- fielding events

This rule is used for:

- `matches_played`
- eligibility thresholds
- recent-form windows
- match-window rankings

---

## 12. Eligibility Rules

### 12.1 Batter Eligibility

**Recommended Default:**

- `matches_played >= 3`
- `balls_faced >= 30`

**Stricter Season-Level:**

- `matches_played >= 5`
- `balls_faced >= 60`

### 12.2 Bowler Eligibility

**Recommended Default:**

- `matches_played >= 3`
- `legal_balls >= 24`

**Stricter Season-Level:**

- `matches_played >= 5`
- `legal_balls >= 60`

### 12.3 All-Rounder Eligibility

A player qualifies as an all-rounder candidate only if they satisfy both batting and bowling contribution rules.

**Minimum Logical Definition:**

```sql
allrounder = has_batting_record AND has_bowling_record
```

**Recommended Season-Level Thresholds:**

- `matches_played >= 5`
- `batting_innings >= 4`
- `bowling_innings >= 4`
- `balls_faced >= 60`
- `legal_balls >= 60`

**Recommended Dynamic Thresholds for General Windows:**

```sql
matches_played  >= max(3, ceil(window_match_count * 0.30))
batting_innings >= max(2, ceil(window_match_count * 0.25))
bowling_innings >= max(2, ceil(window_match_count * 0.25))
balls_faced     >= max(24, window_match_count * 8)
legal_balls     >= max(24, window_match_count * 8)
```

---

## 13. Windowing Rules

All rankings must be computed over a resolved match set.

Supported windows include:

- single season
- multiple seasons
- date range
- all-time
- last N global matches
- last N player matches
- team-specific windows
- opponent-specific windows
- venue-specific windows
- stage-specific windows

> **Mandatory Rule:** Eligibility, normalization, context baselines, and ranking must all be computed from the same selected match set.

---

## 14. Normalization Rules

### 14.1 Preferred Method

Use percentile rank within the selected candidate set (0 to 100 percentile).

### 14.2 Inverse Metrics

For lower-is-better metrics such as:

- economy
- bowling strike rate
- bowling average

Invert before scoring, or rank descending appropriately.

### 14.3 Window-Scoped Normalization

Do not normalize a season-level ranking against all-time data unless explicitly requested.

---

## 15. Ranking Rules

### 15.1 Best Batter

Do not rank batters by runs alone unless the user explicitly asks for total runs.

Preferred batting ranking inputs:

- runs per innings
- batting average
- strike rate
- phase-adjusted strike rate

### 15.2 Best Bowler

Do not rank bowlers by wickets alone unless the user explicitly asks for total wickets.

Preferred bowling ranking inputs:

- wickets per 24 balls
- economy
- bowling strike rate
- dot-ball percentage
- phase-adjusted economy

### 15.3 Best All-Rounder

Do not rank all-rounders by:

- `runs + wickets`
- `runs + wickets * arbitrary_constant`

A valid all-rounder ranking must combine:

- batting quality
- bowling quality
- balance across both disciplines
- optional fielding bonus
- optional awards / impact bonus
- reliability correction

---

## 16. Canonical V1 All-Rounder Scoring Rules

### 16.1 Batting Quality Score

```
BattingQualityScore =
  0.30 * pct(runs_per_innings)
+ 0.25 * pct(batting_average)
+ 0.20 * pct(strike_rate)
+ 0.25 * pct(phase_adjusted_batting_sr_delta)
```

### 16.2 Bowling Quality Score

```
BowlingQualityScore =
  0.25 * pct(wickets_per_24_balls)
+ 0.25 * pct(inv_economy)
+ 0.20 * pct(inv_bowling_strike_rate)
+ 0.15 * pct(dot_ball_pct)
+ 0.15 * pct(phase_adjusted_bowling_econ_delta)
```

### 16.3 Fielding Score

```
FieldingScore =
  0.70 * pct(catches_per_match)
+ 0.30 * pct(runouts_per_match)
```

### 16.4 Balance Score

Use harmonic mean to prevent specialists from dominating all-rounder rankings.

```
BalanceScore =
  2 * BattingQualityScore * BowlingQualityScore
  / (BattingQualityScore + BowlingQualityScore)
```

### 16.5 Reliability Factor

```
ReliabilityFactor =
  sqrt(
    min(1, balls_faced / 120.0)
    * min(1, legal_balls / 120.0)
  )
```

### 16.6 Final Score

```
FinalScoreRaw =
  0.35 * BattingQualityScore
+ 0.35 * BowlingQualityScore
+ 0.20 * BalanceScore
+ 0.05 * FieldingScore
+ 0.05 * pct(player_of_match_count)

FinalScore =
  FinalScoreRaw * (0.85 + 0.15 * ReliabilityFactor)
```

---

### 16.7 ICC-Style Match-Points Formula (Canonical — use this for all-rounder queries)

This is the preferred formula for SQL generation. It approximates the official ICC all-rounder
index without requiring opposition strength or pitch-context data.

**Batting match points (per innings):**

```
BattingPts =
  12 * LN(1 + runs)
  + 8 * not_out_flag
  + 10 * clamp((player_sr / match_sr) - 1, -0.4, 0.6)
  + 4  if team won AND runs >= 30
  + 2  if team won AND runs >= 15
```

Where:
- `player_sr = runs / balls_faced`  (runs per ball)
- `match_sr  = inn_runs / bat_balls` (inning-level runs per ball; `bat_balls` excludes wides)
- Not-out detection: `BOOL_AND(player_dismissed IS DISTINCT FROM batsman)` across all deliveries for that batsman in the match

**Bowling match points (per match spell):**

```
BowlingPts =
  22 * wickets + 6 * LN(1 + wickets)
  + 18 * clamp((match_econ / player_econ) - 1, -0.5, 0.8)
  + 4  * min(legal_balls / 24, 1)
  + 4  if team won AND wickets >= 2
  + 2  if team won AND player_econ < match_econ
```

Where:
- `player_econ = runs_conceded / legal_balls` (runs per ball; `runs_conceded` excludes byes/leg-byes)
- `match_econ  = inn_runs / bowl_balls` (inning-level runs per ball; `bowl_balls` excludes wides and no-balls)
- Bowler dismissal kinds: positive IN list only — `'bowled','caught','caught and bowled','lbw','stumped','hit wicket'`
- If `runs_conceded = 0`, use clamp maximum (`0.8`) — perfect economy earns max bonus

**Season rating (normalised):**

```
MatchRating     = LEAST(1000, GREATEST(0, 300 + 8 * MatchPts))
SeasonRating    = AVG(MatchRating)  -- average across all matches in window
```

**All-rounder index:**

```
AllRounderIndex = BattingRating * BowlingRating / 1000
```

Product is zero if either dimension is zero — a pure batsman or pure bowler cannot rank as an all-rounder.

**Eligibility:**
- `total_balls_faced >= 60` (minimum batting volume)
- `total_legal_balls_bowled >= 60` (minimum bowling volume)

**Key SQL patterns for this formula:**

```sql
-- Inning context (needed for SR and economy adjustment)
inning_ctx AS (
    SELECT match_id, batting_team,
        SUM(total_runs)                                        AS inn_runs,
        COUNT(*) FILTER (WHERE NOT is_wide)                    AS bat_balls,
        COUNT(*) FILTER (WHERE NOT is_wide AND NOT is_no_ball) AS bowl_balls
    FROM deliveries
    GROUP BY match_id, batting_team
)

-- Not-out detection (batting CTE)
BOOL_AND(player_dismissed IS DISTINCT FROM batsman) AS not_out

-- SR adjustment
10.0 * GREATEST(-0.4, LEAST(0.6,
    CASE WHEN bat_balls > 0 AND balls_faced > 0
         THEN (runs::float / balls_faced) / (inn_runs::float / bat_balls) - 1.0
         ELSE 0.0 END))

-- Economy adjustment
18.0 * GREATEST(-0.5, LEAST(0.8,
    CASE WHEN bowl_balls > 0 AND legal_balls > 0
         THEN CASE WHEN runs_conceded = 0 THEN 0.8
                   ELSE (inn_runs::float / bowl_balls)
                        / (runs_conceded::float / legal_balls) - 1.0
              END
         ELSE 0.0 END))

-- Season rating aggregation
AVG(LEAST(1000.0, GREATEST(0.0, 300.0 + 8.0 * (... match_pts ...)))) AS bat_rating
```

---

## 17. Context Adjustment Rules

### 17.1 Batting Phase Baseline

For the selected window:

```
baseline_phase_strike_rate = 100.0 * total_phase_runs / total_phase_balls
```

### 17.2 Bowling Phase Baseline

For the selected window:

```
baseline_phase_economy = 6.0 * total_phase_runs_conceded / total_phase_legal_balls
```

### 17.3 Batting Phase Adjustment

A player's phase-adjusted batting score should compare their phase strike rates against the selected window phase baseline.

### 17.4 Bowling Phase Adjustment

A player's phase-adjusted bowling score should compare their phase economy against the selected window phase baseline.

### 17.5 Fallback Hierarchy for Small Windows

If the selected window is too small, use:

1. exact window baseline
2. season baseline
3. all-time baseline

---

## 18. SQL Generation Rules for NL2SQL

### 18.1 Role-Correct Grouping

- batting → `GROUP BY batsman`
- bowling → `GROUP BY bowler`
- fielding → `GROUP BY fielder_name`

### 18.2 Use Explicit Wicket Logic

Prefer:

```sql
dismissal_kind IN (...)
```

Do not rely only on `NOT IN (...)` unless fully controlled.

### 18.3 Use Schema-Correct Column Mappings

Never invent missing columns such as `total_runs` if not present in schema. Use:

```sql
COALESCE(batsman_runs, 0) + COALESCE(extras, 0)
```

### 18.4 Avoid Grain Mismatch

Do not join player-level aggregates to ball-level or match-level rows without re-aggregation.

### 18.5 Protect Divisions

Use `NULLIF()` or `CASE` to avoid divide-by-zero.

### 18.6 Prefer Window-Scoped Queries

Every ranking query should first resolve the match window, then compute metrics inside that window.

---

## 19. Mandatory Rules

These rules are non-negotiable.

- Bowling wickets must be grouped by bowler.
- Batting runs must be grouped by batsman.
- Legal-ball bowling metrics must exclude wides and no-balls.
- Batter balls faced must exclude wides.
- Bowler wickets must be based on explicit dismissal-kind inclusion.
- `total_runs` must be derived as `batsman_runs + extras`.
- `bowler_runs_conceded` must not include byes / leg-byes.
- All-rounder ranking must not use only runs and wickets.
- Eligibility thresholds must be applied before all-rounder ranking.
- Normalization must be scoped to the selected ranking window.

---

## 20. Recommended Rules

These are strongly recommended defaults.

- Exclude substitute fielders from fielding metrics.
- Exclude super overs unless explicitly requested.
- Use percentile normalization for ranking models.
- Use harmonic mean as the balance score in all-rounder ranking.
- Use phase-adjusted batting and bowling context metrics.
- Use dynamic eligibility thresholds for arbitrary windows.
- Use a canonical player-mapping layer if name variation exists.

---

## 21. Anti-Patterns

The following are invalid or discouraged.

**Invalid:**

- bowling wickets grouped by batsman
- using non-existent `total_runs` column
- using all-rounder score = `runs + wickets`
- counting run out as a bowler wicket
- including wides in balls faced
- including byes / leg-byes in bowler runs conceded

**Discouraged:**

- arbitrary constants like `wickets * 20`
- global normalization for a small custom window
- ranking without eligibility thresholds
- ranking all-rounders without a balance term

---

## 22. Compact Reference Table

| Category | Rule |
|---|---|
| Metric formulas | Strike rate = `100 * runs / balls_faced`; Economy = `6 * runs_conceded / legal_balls`; Bat avg = `runs / outs`; Bowl avg = `runs_conceded / wickets` |
| Eligibility rules | All-rounder must have both batting and bowling contribution, plus minimum thresholds |
| Phase definitions | Powerplay = overs 1–6 cricket view / 0–5 schema view; Middle = 7–15 / 6–14; Death = 16–20 / 15–19 |
| SQL GROUP BY rules | Batting → `GROUP BY batsman`; Bowling → `GROUP BY bowler`; Fielding → `GROUP BY fielder_name` |
| Dismissal filters | Bowler wicket types = `bowled`, `caught`, `caught and bowled`, `lbw`, `stumped`, `hit wicket` |
| Column mappings | `total_runs = batsman_runs + extras`; `bowler_runs_conceded = batsman_runs + wide/no-ball extras only` |
| Valid player filters | Legal delivery = `is_wide = false AND is_no_ball = false`; Batter ball faced = `is_wide = false` |

---

## 23. Minimal SQL Examples

### 23.1 Batting Strike Rate

```sql
SELECT
    batsman,
    SUM(batsman_runs) AS runs,
    COUNT(*) FILTER (WHERE is_wide = false) AS balls_faced,
    ROUND(
        100.0 * SUM(batsman_runs)::numeric
        / NULLIF(COUNT(*) FILTER (WHERE is_wide = false), 0),
        2
    ) AS strike_rate
FROM deliveries
GROUP BY batsman;
```

### 23.2 Bowling Economy

```sql
SELECT
    bowler,
    COUNT(*) FILTER (WHERE is_wide = false AND is_no_ball = false) AS legal_balls,
    SUM(
        COALESCE(batsman_runs, 0)
        + CASE
            WHEN is_wide OR is_no_ball THEN COALESCE(extras, 0)
            ELSE 0
          END
    ) AS runs_conceded,
    ROUND(
        6.0 * SUM(
            COALESCE(batsman_runs, 0)
            + CASE
                WHEN is_wide OR is_no_ball THEN COALESCE(extras, 0)
                ELSE 0
              END
        )::numeric
        / NULLIF(COUNT(*) FILTER (WHERE is_wide = false AND is_no_ball = false), 0),
        2
    ) AS economy_rate
FROM deliveries
GROUP BY bowler;
```

### 23.3 Bowling Wickets

```sql
SELECT
    bowler,
    COUNT(*) FILTER (
        WHERE dismissal_kind IN (
            'bowled',
            'caught',
            'caught and bowled',
            'lbw',
            'stumped',
            'hit wicket'
        )
    ) AS wickets
FROM deliveries
GROUP BY bowler;
```

### 23.4 Basic All-Rounder Candidate Filter

```sql
WITH batting AS (
    SELECT
        batsman AS player,
        COUNT(*) FILTER (WHERE is_wide = false) AS balls_faced
    FROM deliveries
    GROUP BY batsman
),
bowling AS (
    SELECT
        bowler AS player,
        COUNT(*) FILTER (WHERE is_wide = false AND is_no_ball = false) AS legal_balls
    FROM deliveries
    GROUP BY bowler
)
SELECT
    b.player,
    b.balls_faced,
    bw.legal_balls
FROM batting b
JOIN bowling bw
  ON bw.player = b.player
WHERE b.balls_faced >= 60
  AND bw.legal_balls >= 60;
```

### 23.5 Production-Grade All-Rounder Ranking (V2, Windowed)

This is the canonical all-rounder ranking query. It implements the §16 scoring
framework (percentile normalization → batting score → bowling score → harmonic
mean balance → reliability factor → final score) over any configurable match window.

**How to set the window via the `params` CTE:**

| Goal | year_from | year_to | date_from | date_to | last_n_matches |
|---|---|---|---|---|---|
| Single season (2025) | 2025 | 2025 | NULL | NULL | NULL |
| Multiple seasons (2024–2025) | 2024 | 2025 | NULL | NULL | NULL |
| Date range | NULL | NULL | DATE '2025-03-01' | DATE '2025-05-31' | NULL |
| Last N matches overall | NULL | NULL | NULL | NULL | 5 |
| Last N matches in 2025 | 2025 | 2025 | NULL | NULL | 5 |
| All-time | NULL | NULL | NULL | NULL | NULL |

**Note on `batting_average_proxy` when outs = 0:** treated as 999.0 (very high average)
rather than division by zero, so not-out players rank high on this metric as intended.

```sql
WITH params AS (
    SELECT
        NULL::int  AS year_from,
        NULL::int  AS year_to,
        NULL::date AS date_from,
        NULL::date AS date_to,
        NULL::int  AS last_n_matches,
        5::int     AS min_matches,
        4::int     AS min_batting_innings,
        4::int     AS min_bowling_innings,
        60::int    AS min_balls_faced,
        60::int    AS min_legal_balls
),

filtered_matches AS (
    SELECT
        m.*,
        ROW_NUMBER() OVER (
            ORDER BY m.date DESC NULLS LAST, m.match_id DESC
        ) AS recent_rn
    FROM matches m
    CROSS JOIN params p
    WHERE (p.year_from IS NULL OR m.year >= p.year_from)
      AND (p.year_to   IS NULL OR m.year <= p.year_to)
      AND (p.date_from IS NULL OR m.date >= p.date_from)
      AND (p.date_to   IS NULL OR m.date <= p.date_to)
),

scoped_matches AS (
    SELECT fm.*
    FROM filtered_matches fm
    CROSS JOIN params p
    WHERE p.last_n_matches IS NULL
       OR fm.recent_rn <= p.last_n_matches
),

player_match_presence AS (
    SELECT DISTINCT match_id, player
    FROM (
        SELECT px.match_id, px.player_name AS player
        FROM playing_xi px
        JOIN scoped_matches sm ON sm.match_id = px.match_id
        UNION
        SELECT r.match_id, r.player_in AS player
        FROM replacements r
        JOIN scoped_matches sm ON sm.match_id = r.match_id
        WHERE r.player_in IS NOT NULL
        UNION
        SELECT d.match_id, d.batsman AS player
        FROM deliveries d
        JOIN scoped_matches sm ON sm.match_id = d.match_id
        WHERE d.batsman IS NOT NULL
        UNION
        SELECT d.match_id, d.non_striker AS player
        FROM deliveries d
        JOIN scoped_matches sm ON sm.match_id = d.match_id
        WHERE d.non_striker IS NOT NULL
        UNION
        SELECT d.match_id, d.bowler AS player
        FROM deliveries d
        JOIN scoped_matches sm ON sm.match_id = d.match_id
        WHERE d.bowler IS NOT NULL
        UNION
        SELECT d.match_id, d.player_dismissed AS player
        FROM deliveries d
        JOIN scoped_matches sm ON sm.match_id = d.match_id
        WHERE d.player_dismissed IS NOT NULL
        UNION
        SELECT wf.match_id, wf.fielder_name AS player
        FROM wicket_fielders wf
        JOIN scoped_matches sm ON sm.match_id = wf.match_id
        WHERE wf.fielder_name IS NOT NULL
    ) x
),

matches_played AS (
    SELECT player, COUNT(DISTINCT match_id) AS matches_played
    FROM player_match_presence
    GROUP BY player
),

window_size AS (
    SELECT COUNT(*)::int AS total_matches FROM scoped_matches
),

batting_match_balls AS (
    SELECT
        d.match_id,
        d.batsman AS player,
        SUM(COALESCE(d.batsman_runs, 0)) AS runs,
        COUNT(*) FILTER (WHERE COALESCE(d.is_wide, false) = false) AS balls_faced
    FROM deliveries d
    JOIN scoped_matches sm ON sm.match_id = d.match_id
    WHERE d.batsman IS NOT NULL
    GROUP BY d.match_id, d.batsman
),

batting_match_outs AS (
    SELECT
        d.match_id,
        d.player_dismissed AS player,
        COUNT(*) FILTER (
            WHERE d.player_dismissed IS NOT NULL
              AND COALESCE(d.dismissal_kind, '') <> 'retired hurt'
        ) AS outs
    FROM deliveries d
    JOIN scoped_matches sm ON sm.match_id = d.match_id
    WHERE d.player_dismissed IS NOT NULL
    GROUP BY d.match_id, d.player_dismissed
),

batting_match AS (
    SELECT
        COALESCE(b.match_id, o.match_id) AS match_id,
        COALESCE(b.player,   o.player)   AS player,
        COALESCE(b.runs, 0)              AS runs,
        COALESCE(b.balls_faced, 0)       AS balls_faced,
        COALESCE(o.outs, 0)              AS outs,
        CASE
            WHEN COALESCE(b.balls_faced, 0) > 0 OR COALESCE(o.outs, 0) > 0 THEN 1
            ELSE 0
        END AS batting_innings
    FROM batting_match_balls b
    FULL OUTER JOIN batting_match_outs o
      ON b.match_id = o.match_id AND b.player = o.player
),

batting_window AS (
    SELECT
        player,
        COUNT(*) FILTER (WHERE batting_innings = 1)    AS batting_innings,
        SUM(runs)                                       AS runs,
        SUM(balls_faced)                                AS balls_faced,
        SUM(outs)                                       AS outs,
        AVG(runs::numeric) FILTER (WHERE batting_innings = 1) AS runs_per_innings,
        CASE
            WHEN SUM(outs) > 0
            THEN SUM(runs)::numeric / SUM(outs)
            ELSE 999.0  -- treat never-dismissed record as very high average
        END AS batting_average_proxy,
        CASE
            WHEN SUM(balls_faced) > 0
            THEN 100.0 * SUM(runs)::numeric / SUM(balls_faced)
            ELSE 0
        END AS strike_rate
    FROM batting_match
    GROUP BY player
),

bowling_match AS (
    SELECT
        d.match_id,
        d.bowler AS player,
        COUNT(*) FILTER (
            WHERE COALESCE(d.is_wide, false) = false
              AND COALESCE(d.is_no_ball, false) = false
        ) AS legal_balls,
        SUM(
            COALESCE(d.batsman_runs, 0) +
            CASE
                WHEN COALESCE(d.is_wide, false) = true
                  OR COALESCE(d.is_no_ball, false) = true
                THEN COALESCE(d.extras, 0)
                ELSE 0
            END
        ) AS runs_conceded,
        COUNT(*) FILTER (
            WHERE COALESCE(d.dismissal_kind, '') IN (
                'bowled', 'caught', 'caught and bowled',
                'lbw', 'stumped', 'hit wicket'
            )
        ) AS wickets,
        COUNT(*) FILTER (
            WHERE COALESCE(d.is_wide, false) = false
              AND COALESCE(d.is_no_ball, false) = false
              AND (COALESCE(d.batsman_runs, 0) + COALESCE(d.extras, 0)) = 0
        ) AS dot_balls
    FROM deliveries d
    JOIN scoped_matches sm ON sm.match_id = d.match_id
    WHERE d.bowler IS NOT NULL
    GROUP BY d.match_id, d.bowler
),

bowling_window AS (
    SELECT
        player,
        COUNT(*) FILTER (WHERE legal_balls > 0) AS bowling_innings,
        SUM(legal_balls)    AS legal_balls,
        SUM(runs_conceded)  AS runs_conceded,
        SUM(wickets)        AS wickets,
        SUM(dot_balls)      AS dot_balls,
        CASE
            WHEN SUM(legal_balls) > 0
            THEN 6.0 * SUM(runs_conceded)::numeric / SUM(legal_balls)
            ELSE NULL
        END AS economy,
        CASE
            WHEN SUM(wickets) > 0
            THEN SUM(legal_balls)::numeric / SUM(wickets)
            ELSE NULL
        END AS bowling_strike_rate,
        CASE
            WHEN SUM(legal_balls) > 0
            THEN 24.0 * SUM(wickets)::numeric / SUM(legal_balls)
            ELSE 0
        END AS wickets_per_24_balls,
        CASE
            WHEN SUM(legal_balls) > 0
            THEN 100.0 * SUM(dot_balls)::numeric / SUM(legal_balls)
            ELSE 0
        END AS dot_ball_pct
    FROM bowling_match
    GROUP BY player
),

candidates AS (
    SELECT
        mp.player,
        mp.matches_played,
        bwin.total_matches,
        bw.batting_innings,  bw.runs,         bw.balls_faced,
        bw.outs,             bw.runs_per_innings,
        bw.batting_average_proxy,              bw.strike_rate,
        bo.bowling_innings,  bo.legal_balls,   bo.runs_conceded,
        bo.wickets,          bo.dot_balls,     bo.economy,
        bo.bowling_strike_rate, bo.wickets_per_24_balls, bo.dot_ball_pct
    FROM matches_played mp
    JOIN batting_window  bw   ON bw.player = mp.player
    JOIN bowling_window  bo   ON bo.player = mp.player
    CROSS JOIN window_size bwin
    CROSS JOIN params p
    WHERE mp.matches_played  >= p.min_matches
      AND bw.batting_innings >= p.min_batting_innings
      AND bo.bowling_innings >= p.min_bowling_innings
      AND bw.balls_faced     >= p.min_balls_faced
      AND bo.legal_balls     >= p.min_legal_balls
),

normalized AS (
    SELECT
        c.*,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.runs_per_innings)          AS pct_runs_per_innings,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.batting_average_proxy)     AS pct_batting_average,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.strike_rate)               AS pct_strike_rate,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.wickets_per_24_balls)      AS pct_wkts_per_24,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.economy DESC NULLS LAST)   AS pct_inv_economy,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.bowling_strike_rate DESC NULLS LAST) AS pct_inv_bowling_sr,
        100.0 * PERCENT_RANK() OVER (ORDER BY c.dot_ball_pct)              AS pct_dot_ball_pct
    FROM candidates c
),

scores AS (
    SELECT
        n.*,
        (0.35 * n.pct_runs_per_innings +
         0.30 * n.pct_batting_average  +
         0.35 * n.pct_strike_rate)          AS batting_score,
        (0.35 * n.pct_wkts_per_24       +
         0.30 * n.pct_inv_economy       +
         0.20 * n.pct_inv_bowling_sr    +
         0.15 * n.pct_dot_ball_pct)         AS bowling_score
    FROM normalized n
),

final_rankings AS (
    SELECT
        s.*,
        CASE
            WHEN (s.batting_score + s.bowling_score) > 0
            THEN 2.0 * s.batting_score * s.bowling_score
                 / (s.batting_score + s.bowling_score)
            ELSE 0
        END AS balance_score,
        SQRT(
            LEAST(1.0, s.balls_faced::numeric / 120.0) *
            LEAST(1.0, s.legal_balls::numeric / 120.0)
        ) AS reliability_factor
    FROM scores s
)

SELECT
    RANK() OVER (
        ORDER BY
            (0.35 * batting_score + 0.35 * bowling_score +
             0.30 * CASE WHEN (batting_score + bowling_score) > 0
                         THEN 2.0 * batting_score * bowling_score
                              / (batting_score + bowling_score)
                         ELSE 0 END
            ) * (0.85 + 0.15 * reliability_factor) DESC,
            balance_score DESC,
            bowling_score DESC,
            batting_score DESC,
            matches_played DESC,
            player ASC
    ) AS rank_in_window,

    player,
    total_matches AS matches_in_selected_window,
    matches_played,
    batting_innings,
    bowling_innings,
    runs,
    balls_faced,
    ROUND(batting_average_proxy, 2)   AS batting_average,
    ROUND(strike_rate, 2)             AS strike_rate,
    wickets,
    legal_balls,
    ROUND(economy, 2)                 AS economy,
    ROUND(bowling_strike_rate, 2)     AS bowling_strike_rate,
    ROUND(wickets_per_24_balls, 2)    AS wickets_per_24_balls,
    ROUND(dot_ball_pct, 2)            AS dot_ball_pct,
    ROUND(batting_score, 2)           AS batting_score,
    ROUND(bowling_score, 2)           AS bowling_score,
    ROUND(balance_score, 2)           AS balance_score,
    ROUND(reliability_factor, 3)      AS reliability_factor,
    ROUND(
        (0.35 * batting_score + 0.35 * bowling_score + 0.30 * balance_score)
        * (0.85 + 0.15 * reliability_factor),
        2
    ) AS final_score
FROM final_rankings
ORDER BY rank_in_window
LIMIT 5;
```

---

## 24. NL2SQL Policy Summary

When generating cricket SQL:

- identify whether the question is about batting, bowling, fielding, or all-round performance
- resolve the match window first
- use schema-correct derived columns
- apply role-correct grouping
- apply correct dismissal attribution
- apply eligibility rules before ranking
- avoid arbitrary heuristics unless explicitly labeled as rough
- for all-rounders, use the ICC-style match-points formula (§16.7): batting_rating * bowling_rating / 1000

---

## 25. Future Improvements

The following enhancements are recommended for future versions:

- canonical player identity mapping table
- explicit super-over handling
- venue-adjusted and opponent-adjusted baselines
- score-state materialization (`runs_before_ball`, `wickets_before_ball`, `balls_remaining`)
- player role labels (batter / bowler / all-rounder / keeper)
- match-quality and opposition-strength adjustments

---

## 26. Change Log

**v1.0**

- Initial repo-ready rules spec
- Added canonical formulas
- Added dismissal and grouping rules
- Added all-rounder ranking framework
- Added NL2SQL policy guidance
