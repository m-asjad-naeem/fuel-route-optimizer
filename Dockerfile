FROM python:3.13-slim

# proj-bin/libproj-dev: required by pyproj (geospatial projection, Phase 5).
# gcc: required to build pyproj/psycopg from source on this base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends proj-bin libproj-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
