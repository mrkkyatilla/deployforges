var builder = WebApplication.CreateBuilder(args);
builder.Services.AddEndpointsApiExplorer();
var app = builder.Build();

app.MapGet("/health", () => Results.Ok(new { status = "ok" }));
app.Run();
