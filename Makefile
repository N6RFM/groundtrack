.PHONY: build check check-live status doctor clean

build:
	./regen_all.sh

check:
	python3 preflight.py

check-live:
	python3 preflight.py --live

status:
	python3 doctor.py --status

doctor:
	python3 doctor.py

clean:
	rm -rf __pycache__ flowgraphs/__pycache__ */flowgraphs/__pycache__ *.pyc
