import os
import string
from functools import wraps

from flask import Flask, request, redirect, jsonify
import redis
import psycopg2

app = Flask(__name__)

ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase

cache = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True,
)


def get_db():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "db"),
        database=os.environ.get("POSTGRES_DB", "urlshortener"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "postgres"),
    )


def base62_encode(num):
    if num == 0:
        return ALPHABET[0]
    result = []
    while num > 0:
        num, remainder = divmod(num, 62)
        result.append(ALPHABET[remainder])
    return "".join(reversed(result))


def base62_decode(code):
    num = 0
    for char in code:
        num = num * 62 + ALPHABET.index(char)
    return num

def rate_limit(max_requests=10, window=60):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Get the client's IP address
            client_ip = request.remote_addr
            # Create a unique Redis key for this IP
            key = f"rate_limit:{client_ip}"
            # Check current request count
            current = cache.get(key)
            if current and int(current) >= max_requests:
                return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
            # Increment counter and set expiration atomically
            pipe = cache.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            pipe.execute()
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@app.route("/shorten", methods=["POST"])
@rate_limit(max_requests=10, window=60)
def shorten_url():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "Missing url field"}), 400

    long_url = data["url"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO urls (long_url) VALUES (%s) RETURNING id", (long_url,))
    url_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    short_code = base62_encode(url_id)
    cache.set(short_code, long_url)

    return jsonify({"short_url": f"http://localhost:5000/{short_code}", "short_code": short_code}), 201


@app.route("/<short_code>")
def redirect_url(short_code):
    long_url = cache.get(short_code)

    if long_url:
        print("CACHE HIT", flush=True)
        return redirect(long_url, code=302)

    print("CACHE MISS - querying database", flush=True)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT long_url FROM urls WHERE id = %s", (base62_decode(short_code),))
    result = cur.fetchone()
    cur.close()
    conn.close()

    if not result:
        return jsonify({"error": "Short URL not found"}), 404

    long_url = result[0]
    cache.set(short_code, long_url)
    return redirect(long_url, code=302)


@app.route("/health")
def health():
    checks = {"web": "ok"}
    healthy = True

    try:
        cache.ping()
        checks["redis"] = "ok"
    except redis.RedisError as error:
        checks["redis"] = f"error: {error}"
        healthy = False

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        checks["postgres"] = "ok"
    except psycopg2.Error as error:
        checks["postgres"] = f"error: {error}"
        healthy = False
    finally:
        if conn is not None:
            conn.close()

    return jsonify({
        "status": "healthy" if healthy else "unhealthy",
        "checks": checks,
    }), 200 if healthy else 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
