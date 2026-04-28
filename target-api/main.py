from fastapi import FastAPI
import uvicorn

app = FastAPI()
healthy = True  # we'll toggle this with /chaos and /recover

@app.get("/health")
def health():
    if healthy:
        return {"status": "ok"}
    return {"status": "unhealthy"}, 500

@app.post("/chaos")
def chaos():
    global healthy
    healthy = False
    return {"message": "chaos triggered — /health now returns 500"}

@app.post("/recover")
def recover():
    global healthy
    healthy = True
    return {"message": "recovered — /health now returns 200"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
