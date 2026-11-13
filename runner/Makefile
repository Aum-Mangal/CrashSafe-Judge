# Builds judge-runner as a static binary, so it runs in any sandbox image.
CFLAGS = -O2 -Wall -Wextra -Werror -static

judge-runner: judge_runner.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f judge-runner

.PHONY: clean
