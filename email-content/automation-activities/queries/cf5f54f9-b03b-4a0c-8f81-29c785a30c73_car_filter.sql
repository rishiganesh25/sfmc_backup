select [coustemer id], [email id], [first name], [last name], [brand name], model, varient, [delivery date],
count( [brand name]) as brand_count
from car
group by [coustemer id], [email id], [first name], [last name], [brand name], model, varient, [delivery date]