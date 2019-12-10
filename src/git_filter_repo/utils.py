def decode(bytestr):
    "Try to convert bytestr to utf-8 for outputting as an error message."
    return bytestr.decode("utf-8", "backslashreplace")
