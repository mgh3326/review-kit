"""Setuptools compatibility entry point for older pip frontends."""
from setuptools import setup
from pathlib import Path


readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")


setup(
    name="review-kit",
    version="1.0.0",
    description="Prepare, package and share traceable review material",
    long_description=readme,
    long_description_content_type="text/markdown",
    author="review-kit contributors",
    license="MIT",
    keywords=["ffmpeg", "video", "review", "sharing", "mp4"],
    classifiers=[
        "Environment :: Console",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
    ],
    packages=["review_kit"],
    py_modules=["prep_video"],
    python_requires=">=3.9",
    install_requires=[],
    entry_points={
        "console_scripts": [
            "prep-video=prep_video:main",
            "review-kit=review_kit.cli:main",
        ],
    },
)
