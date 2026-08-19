# Python 3.14 compatibility monkeypatch for Django's BaseContext.__copy__
try:
    from django.template import context

    def _safe_base_context_copy(self):
        duplicate = self.__class__.__new__(self.__class__)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = self.dicts[:]
        return duplicate

    context.BaseContext.__copy__ = _safe_base_context_copy
except Exception:
    pass
