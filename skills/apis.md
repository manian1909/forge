---
name: apis
description: free web APIs for live data (weather, money, crypto, stocks, facts, GitHub, news, holidays, quakes)
triggers: weather|temperature|forecast|\brain(ing|fall)?\b|\baqi\b|air quality|exchange rate|currency|convert .* (to|into) [a-z]{3}\b|\b(usd|inr|eur|gbp)\b|bitcoin|crypto|ethereum|\bstocks?\b|share price|nifty|sensex|wikipedia|who is|github|\brepo\b|hacker news|holiday|earthquake|time in|time zone|\bapi\b|live data|latest|right now|today's|current price|mutual fund|\bnav\b|arxiv|papers? on|books? (by|about)
---
Live data: call http_request (GET runs without asking). These need NO key. Answer only from what the call returns, with units and the time of the data.
- Place -> coordinates: https://geocoding-api.open-meteo.com/v1/search  params name=Delhi, count=1
- Weather: https://api.open-meteo.com/v1/forecast  params latitude, longitude, current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code, daily=temperature_2m_max,temperature_2m_min,precipitation_sum, timezone=auto, forecast_days=3
- Air quality: https://air-quality-api.open-meteo.com/v1/air-quality  params latitude, longitude, current=pm2_5,pm10,us_aqi
- Currency (ECB rates): https://api.frankfurter.dev/v1/latest  params base=USD, symbols=INR,EUR   (history: /v1/2025-01-01..2025-01-31)
- Crypto: https://api.coingecko.com/api/v3/simple/price  params ids=bitcoin,ethereum, vs_currencies=usd,inr, include_24hr_change=true
- Stocks and indices (unofficial Yahoo): https://query1.finance.yahoo.com/v8/finance/chart/AAPL  params range=5d, interval=1d. Indian shares end in .NS (RELIANCE.NS), Nifty is ^NSEI, Sensex ^BSESN. Price is chart.result[0].meta.regularMarketPrice.
- Indian mutual fund NAV: https://api.mfapi.in/mf/search?q=parag  then https://api.mfapi.in/mf/<schemeCode>/latest
- Wikipedia summary: https://en.wikipedia.org/api/rest_v1/page/summary/<Title_With_Underscores>  (search: https://en.wikipedia.org/w/api.php params action=opensearch, search=..., limit=3, format=json)
- GitHub: https://api.github.com/repos/<owner>/<repo> ; search: https://api.github.com/search/repositories params q=..., sort=stars, per_page=5
- Hacker News: https://hacker-news.firebaseio.com/v0/topstories.json then /v0/item/<id>.json (fetch only the first 5 ids)
- Public holidays: https://date.nager.at/api/v3/PublicHolidays/<year>/<CC>  (US, GB, DE...; empty for some countries)
- Time in a zone: https://timeapi.io/api/Time/current/zone  params timeZone=Asia/Kolkata
- Earthquakes: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson
- Books: https://openlibrary.org/search.json  params q=..., limit=5 ; papers: https://export.arxiv.org/api/query params search_query=all:<words>, max_results=5 (XML)
- Your public IP and city: https://ipapi.co/json/
APIs that need a key: write it as {{NAME}}, e.g. params appid={{OPENWEATHER_KEY}} or headers Authorization="Bearer {{GITHUB_TOKEN}}". If the key is missing, the tool says so: tell the user the /key command it printed.
For big responses or several calls (tables, averages, saving to Excel), use run_python with httpx instead and print only what is needed.
