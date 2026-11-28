from pydantic import BaseModel, ConfigDict, Field


class InternetmarkeConnect(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=1, max_length=22)  # the API's own limit
    sender_name: str = Field(..., min_length=1, max_length=50)
    sender_street: str = Field(..., min_length=1, max_length=50)
    sender_zip: str = Field(..., min_length=3, max_length=10)
    sender_city: str = Field(..., min_length=1, max_length=35)
    accept_terms: bool = False


class InternetmarkePageFormat(BaseModel):
    page_format_id: int


class InternetmarkeBuy(BaseModel):
    product_code: int
    # The price the seller saw and agreed to.
    price_cents: int = Field(..., gt=0)
    accept_terms: bool = False
