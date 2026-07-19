from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="alice_shop_floor",
    version="0.1.0",
    description="ALICE Shop Floor Layer — AI-powered MES modules for ZAZFIT on ERPNext",
    author="Athlettia LLC",
    author_email="frankoy@athlettia.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
