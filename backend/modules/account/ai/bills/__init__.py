"""Synthetic bill documents for the ``account.bill_ocr`` evaluation (2-i).

``corpus`` draws the bills (vendors, languages, layouts, lines, taxes, the coding a bookkeeper would choose)
from a fixed seed; ``render`` turns one into a digital PDF (HTML → WeasyPrint), a degraded scan (Pillow:
rotation, blur, noise, JPEG) or an image-only PDF. Documents are rendered at evaluation time from the gold
files in ``ai/evals/bills/`` (nothing binary is committed), deterministically.
"""
