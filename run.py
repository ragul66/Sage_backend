import uvicorn
from app.core.config import settings

if __name__ == "__main__":
    # We host on 0.0.0.0 so it binds to the LAN IP, allowing the mobile device to connect.
    # Reload is set to True to facilitate rapid development hot-reloading.
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT, reload=True)
