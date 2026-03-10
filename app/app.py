from factory import create_app
from settings import get_env


app = create_app()


if __name__ == "__main__":
    app.run(host=get_env("FLASK_HOST", "0.0.0.0"), port=int(get_env("PORT", "8000")))
