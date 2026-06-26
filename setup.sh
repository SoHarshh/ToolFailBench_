#!/usr/bin/env bash

rm -rf .venv/

uv venv .venv --python=3.12

source .venv/bin/activate

uv pip install -r requirements.txt

echo "Done"
