/* Claude Duo's app executable: runs menubar.py with Homebrew's Python inside this process (no exec), so macOS
   sees the whole app as one signed program and lets it put its icon in the menu bar. */
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef PYHOME
#error "build with -DPYHOME=\"...\""
#endif

int main(int argc, char **argv) {
    const char *home = getenv("HOME");
    char dir[1024], path[4096], script[1200];
    if (!home) return 1;
    snprintf(dir, sizeof dir, "%s/ClaudeUsage", home);  /* not the Desktop: that folder is privacy-protected */
    if (chdir(dir) != 0) { fprintf(stderr, "Claude Duo: %s is missing\n", dir); return 1; }
    snprintf(path, sizeof path, "%s:%s/.venv/lib/python3.14/site-packages", dir, dir);
    setenv("PYTHONHOME", PYHOME, 1);
    setenv("PYTHONPATH", path, 1);
    snprintf(script, sizeof script, "%s/menubar.py", dir);
    char **args = calloc(argc + 2, sizeof *args);
    args[0] = argv[0];
    args[1] = script;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    return Py_BytesMain(argc + 1, args);
}
