SECRET_KEY = "dev-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "helloworld.urls"
WSGI_APPLICATION = "helloworld.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth"]
