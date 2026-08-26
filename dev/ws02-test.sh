#!/usr/bin/env bash
set -e
git push -q origin master
git push -q github master 2>/dev/null || true
ssh ws02 'cd ~/repos/ccm && git pull -q --ff-only && python3 -m unittest discover -s tests'
