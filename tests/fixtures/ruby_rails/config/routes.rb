Rails.application.routes.draw do
  get "health", to: "health#index"
  root "home#index"
end
