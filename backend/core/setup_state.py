class SetupState:
    """Singleton class to track whether initial setup is required.

    This is a simple in-memory flag that is set to False once an admin user is
    created.  It is never re-checked against the database for performance, so a
    server restart is required after setup completion.
    """

    __slots__ = ("_needs_setup",)

    def __init__(self):
        self._needs_setup: bool = True

    @property
    def needs_setup(self) -> bool:
        return self._needs_setup

    @needs_setup.setter
    def needs_setup(self, value: bool) -> None:
        self._needs_setup = value


# singleton instance to be imported and used across the app.
setup_state = SetupState()
