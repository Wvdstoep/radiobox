from app import create_app

app = create_app()


def start_app():
    return app


if __name__ == '__main__':
    app = start_app()
    app.run(host='0.0.0.0', port=5001, debug=True)
