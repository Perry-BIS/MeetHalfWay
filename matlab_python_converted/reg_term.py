def reg_term(da1: int, da2: int, dl1: int, dl2: int) -> int:
    """Equivalent of the MATLAB reg_term function."""
    return (da1 - da2) ** 2 + (dl1 - dl2) ** 2
