"""The stages of one analysis run, one module each, over the Run in research/pipeline.py (which keeps the
stage table, the saved state and the bookkeeping): plan, investigate (search, read), compute (calculate),
mechanism, report, assess; submit records what goes out; windows chooses what of a record is sent.
Each stage function takes the run as its first argument."""


class Failed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
