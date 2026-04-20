# EMTA Transit Tracker: Interview & Architecture Challenges

## The Bronze/Silver/Gold Architecture

**Why do we use the Bronze/Silver/Gold schema?**
Based on the project architecture, it uses the Medallion Architecture to progressively refine data as it flows from the live API into analytical insights.

**1. The Bronze Layer (Raw Extraction)**
*   **Purpose:** It serves as a pure, unaltered data dump.
*   **Why we use it:** It contacts the EMTA API and safely stores the exact, raw dictionary response (including a raw JSON backup column). By doing this, if your cleaning logic breaks later on, you never lose the original historical data. It acts as an undisputable historical archive.

**2. The Silver Layer (Cleaning & Translation)**
*   **Purpose:** It takes the noisy Bronze data and translates it into a human-readable format.
*   **Why we use it:** It standardizes the data so downstream users or applications don't have to write complex logic. It calculates `delay_bucket` (early/on-time/late), shifts timestamps timezone from UTC to Eastern time, and extracts simple strings like the `hour` and `day_name`.

**3. The Gold Layer (High-Value Aggregation)**
*   **Purpose:** It serves highly compressed, pre-calculated "scoreboard" metrics.
*   **Why we use it:** Instead of forcing AI or dashboards to parse millions of individual bus pings on the fly (which would be slow and expensive), the Gold layer pre-calculates the final `reliability_score` grouped by route, hour, and day. This allows your Weekly AI agent to simply query the fastest, most optimized view of the pipeline while saving token costs limit sizes.

---

## Mitigating AI Token Costs

**Why won't running the daily agent cost a lot of tokens since it gets data from the massive Silver table?**
Even though the Daily AI agent queries the `silver_arrivals` table (which has millions of individual bus rows), it uses two distinct strategies to prevent sending all that data to the AI model and running up token costs:

**1. Database Pre-Aggregation**
Instead of downloading every Silver row and sending it to Claude, the Python script forces your PostgreSQL database to do the math first. It uses `GROUP BY route_name` and calculates the `on_time_pct` and `avg_delay` right inside the SQL query.

**2. Strict Limiting (Limit 40)**
Once the database calculates the summary for every route that day, the query uses `ORDER BY on_time_pct ASC` and `LIMIT 40`.
This means that out of the potentially thousands of buses that run every day, the python script only downloads a tiny, summarized list of the 40 worst-performing routes. By the time the data is formatted into the prompt, it is less than 50 lines of text, ensuring the token payload sent to Claude remains extremely small.

---

## The Reliability Score & Recruiter Challenges

### The Simple Explanation: The Pizza Delivery Metaphor

Imagine you run a pizza shop and promise delivery in 30 minutes. Let's say you want to grade two of your drivers, Driver A and Driver B.

Both drivers deliver the pizza "on-time" 80% of the time. If you only tracked a basic "On-Time Percentage", they would get the exact same score.

But there's a catch:
*   When Driver A is late, they are 2 minutes late. (The pizza is still hot).
*   When Driver B is late, they are 45 minutes late. (The pizza is ice cold and ruined).

Your EMTA tracker's **Reliability Score** solves this by splitting the grade into two parts:
1.  **70% of the grade is just showing up:** Did the bus get there on time? Yes or No. (This is the pure On-Time Percentage).
2.  **30% of the grade is damage control:** If the bus *was* late, how painful was the wait? If it was only a little late, you don't lose many points. If it ran 15+ minutes late, you lose all the points in this category.

Because you blended the score, "Driver A" mathematically gets a much higher Reliability Score in your data warehouse than "Driver B", accurately reflecting the real-life passenger experience.

### The Recruiter Challenge (Interview Pushback)

If you explain this in an interview, a tough Data Engineering or Product Management interviewer will likely stress-test your logic.

**Recruiter Challenge 1: The 15-Minute Cap**
> *"I see you capped the delay penalty at 15 minutes. Why 15? If a bus is 45 minutes late, isn't that vastly worse for a passenger than a bus that is 15 minutes late? Why does your math stop penalizing them at 15?"*

**Recruiter Challenge 2: The 70/30 Split**
> *"Why did you arbitrarily choose a 70/30 weight? Why not 50/50? Isn't it a bit subjective to decide that being on time is exactly 2.3 times more important than the severity of the delay?"*

**Recruiter Challenge 3: Edge Cases (The "Early" Penalty)**
> *"Wait, your delay penalty uses `AVG(adherence_minutes)`. If a bus is 10 minutes 'early', the adherence is -10. In your math, `GREATEST(AVG, 0)` forces early buses to 0. You aren't penalizing a bus for leaving riders behind by arriving early! You're treating 'early' the exact same as 'perfectly on time' in the 30% bucket. Is that intentional?"*

### How You Defend It (Your Answer)

**Defending the 15-Minute Cap:**
*"In public transit, an irregular delay beyond 15 minutes operates functionally the same as a ghost bus—the passenger has already given up, called an Uber, or completely missed their connection. Capping the mathematical penalty at 15 minutes prevents extreme outliers (like a bus breaking down and reading 300 minutes late) from completely destroying the algorithm's average, while accurately reflecting that past 15 minutes, customer satisfaction is already at zero."*

**Defending the 70/30 Split:**
*"It is intentionally weighted heavier toward the binary 'On-Time' metric to align with transit industry standards. Most agencies exclusively use simple OTP (On-Time Percentage). By keeping it heavily weighted at 70%, the metric remains familiar to stakeholders, but injecting the 30% penalty allows my model to break ties and act as a qualitative tie-breaker that a standard OTP metric lacks."*

**Defending the "Early" Edge Case:**
*"You're exactly right—my current 30% penalty bucket zeroes out early buses. However, they are already heavily penalized in the 70% bucket because they completely lose the binary 'On-Time' status for that ping. In the future, I could adjust the algorithm to use absolute values (`ABS`) to penalize the severity of early departures in the 30% bucket as well, but for Version 1, I prioritized penalizing delays since that is the primary passenger complaint in Erie."*
